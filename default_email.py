#!/usr/bin/env python3
"""Bulk update Yandex 360 default_email using only the Python standard library."""
import argparse
import csv
import getpass
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
FIELDS = ["email", "id", "name", "default_email"]
BASE_URL = "https://cloud-api.yandex.net/v1/directory/organizations"


class UserError(Exception):
    pass


class ApiError(UserError):
    def __init__(self, message, status=None, uncertain=False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, org_id, token):
        self.url = f"{BASE_URL}/{org_id}/users"
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, method, suffix="", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(self.url + suffix, data=data, method=method,
                          headers={"Authorization": f"OAuth {self.token}",
                                   "Content-Type": "application/json",
                                   "Accept": "application/json"})
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=30) as response:
                    result = json.load(response)
                if not isinstance(result, dict):
                    raise ValueError("Not an object")
                return result
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After", "")
                error.close()
                if method == "GET" and (status == 429 or status >= 500) and attempt < 2:
                    delay = int(retry_after) if retry_after.isdigit() else 2 ** attempt
                    time.sleep(min(delay, 60))
                    continue
                raise ApiError(f"HTTP {status}", status,
                               uncertain=method == "PATCH" and status >= 500) from None
            except (URLError, TimeoutError, OSError, HTTPException):
                if method == "GET" and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError("Сетевая ошибка или таймаут", uncertain=method == "PATCH") from None
            except (ValueError, UnicodeError):
                raise ApiError("Некорректный JSON в ответе API", uncertain=method == "PATCH") from None

    def users(self):
        users, offset, total = {}, 0, None
        while True:
            page = self.request("GET", "?" + urlencode({"limit": 100, "offset": offset}))
            items, count, limit = page.get("items"), page.get("total"), page.get("limit")
            if (not isinstance(items, list) or type(count) is not int or count < 0
                    or type(limit) is not int or limit <= 0 or page.get("offset") != offset):
                raise UserError("Некорректная структура страницы API")
            if total is not None and count != total:
                raise UserError("Число сотрудников изменилось во время выгрузки. Повторите запуск")
            total = count
            for user in items:
                if not isinstance(user, dict):
                    raise UserError("Некорректная запись сотрудника")
                uid = str(user.get("id", ""))
                if not re.fullmatch(r"[0-9]+", uid) or uid in users:
                    raise UserError("Некорректный или повторяющийся UID в ответе API")
                users[uid] = user
            if len(users) == total:
                return users
            if not items or len(users) > total or len(items) != limit:
                raise UserError("Неполная или несогласованная выгрузка API")
            offset += limit

    def patch(self, uid, email):
        return self.request("PATCH", f"/{uid}", {"default_email": email})


def project_path(value):
    path = (ROOT / value).resolve()
    if path.parent != ROOT:
        raise UserError("CSV должен находиться непосредственно в папке скрипта")
    return path


def safe_cell(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def user_row(user):
    name = user.get("name") or {}
    if not isinstance(name, dict) or not isinstance(user.get("email"), str):
        raise UserError("В ответе API отсутствует email или некорректно name")
    full_name = " ".join(str(name.get(key) or "") for key in ("last", "first", "middle")).strip()
    return {"email": safe_cell(user["email"]), "id": str(user["id"]),
            "name": safe_cell(full_name), "default_email": ""}


def private_open(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8-sig", newline="")


def export_users(client, org_id):
    users = client.users()
    rows = [user_row(user) for user in users.values()]
    name = datetime.now(timezone.utc).strftime("users_%Y%m%dT%H%M%SZ_") + uuid4().hex[:8] + ".csv"
    path = ROOT / name
    snapshot = {"org_id": org_id, "users": {
        row["id"]: {**row, "current_default_email": users[row["id"]].get("default_email", "")}
        for row in rows}}
    with private_open(path.with_suffix(".snapshot.json")) as stream:
        json.dump(snapshot, stream, ensure_ascii=False, indent=2)
    with private_open(path) as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_plan(path, org_id, current):
    with path.with_suffix(".snapshot.json").open(encoding="utf-8-sig") as stream:
        snapshot = json.load(stream)
    if snapshot.get("org_id") != org_id or not isinstance(snapshot.get("users"), dict):
        raise UserError("Снимок CSV относится к другой организации или повреждён")
    baseline = snapshot["users"]
    plan, seen = [], set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=";")
        if reader.fieldnames != FIELDS:
            raise UserError("Ожидается заголовок email;id;name;default_email")
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise UserError(f"Строка {line}: неверное число колонок")
            uid = row["id"]
            original = baseline.get(uid)
            if uid in seen or not isinstance(original, dict):
                raise UserError(f"Строка {line}: неизвестный или повторяющийся UID")
            seen.add(uid)
            if any(row[key] != original.get(key) for key in ("id", "email", "name")):
                raise UserError(f"Строка {line}: изменены служебные колонки")
            desired = row["default_email"].strip()
            if not desired:
                plan.append((uid, "", "skipped_empty"))
                continue
            if not re.fullmatch(r"[^\s@;<>]+@[^\s@;<>]+", desired) or len(desired) > 254:
                raise UserError(f"Строка {line}: некорректный default_email")
            user = current.get(uid)
            if user is None or safe_cell(user.get("email", "")) != row["email"]:
                raise UserError(f"Строка {line}: сотрудник удалён или email изменился")
            actual = user.get("default_email", "")
            if desired == actual:
                plan.append((uid, desired, "skipped_equal"))
            elif actual != original.get("current_default_email", ""):
                raise UserError(f"Строка {line}: default_email изменён после выгрузки; нужна новая выгрузка")
            else:
                plan.append((uid, desired, "pending"))
    return plan


def execute(client, plan, report):
    counts = {"success": 0, "failed": 0, "unknown": 0, "skipped": 0}
    # Each durable 'started' without a matching outcome means an uncertain request.
    with private_open(report) as stream:
        def record(uid, desired, status, detail=""):
            json.dump({"time": datetime.now(timezone.utc).isoformat(), "id": uid,
                       "default_email": desired, "status": status, "detail": detail},
                      stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for index, (uid, desired, state) in enumerate(plan, 1):
            if state != "pending":
                counts["skipped"] += 1
                record(uid, desired, state)
                continue
            record(uid, desired, "started")
            try:
                user = client.patch(uid, desired)
                if str(user.get("id")) != uid or user.get("default_email") != desired:
                    raise ApiError("Ответ PATCH не подтверждает целевое значение", uncertain=True)
            except ApiError as error:
                state = "unknown" if error.uncertain else "failed"
                counts[state] += 1
                record(uid, desired, state, str(error))
                print(f"[{index}/{len(plan)}] UID {uid}: {state} ({error})")
                if error.status in (401, 403, 429) or error.uncertain:
                    print("Пакет остановлен. Оставшиеся строки не отправлены; проверьте отчёт.")
                    return 1
            else:
                counts["success"] += 1
                record(uid, desired, "success")
                print(f"[{index}/{len(plan)}] UID {uid}: success")
    print("Готово: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 1 if counts["failed"] or counts["unknown"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Массовая смена default_email в Яндекс 360")
    parser.add_argument("--org-id", required=True, help="ID организации")
    parser.add_argument("--apply", metavar="CSV", help="Продолжить с ранее выгруженным CSV")
    parser.add_argument("--export-only", action="store_true", help="Только выгрузить CSV")
    parser.add_argument("--dry-run", action="store_true", help="Проверить CSV без PATCH")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9]+", args.org_id):
        parser.error("--org-id должен состоять из цифр")
    if args.export_only and (args.apply or args.dry_run):
        parser.error("--export-only нельзя сочетать с --apply или --dry-run")
    token = os.environ.get("Y360_TOKEN") or getpass.getpass("OAuth-токен (ввод скрыт): ")
    if not token.strip():
        raise UserError("Токен не задан")
    client = Client(args.org_id, token.strip())
    path = project_path(args.apply) if args.apply else export_users(client, args.org_id)
    print(f"CSV: {path}")
    if args.export_only:
        return 0
    if not args.apply:
        print("Заполните колонку default_email, сохраните CSV (UTF-8, разделитель ;) и закройте редактор.")
        if input("Введите READY для чтения файла: ").strip() != "READY":
            print("Отменено. CSV сохранён; продолжить можно через --apply.")
            return 0
    plan = read_plan(path, args.org_id, client.users())
    pending = sum(state == "pending" for _, _, state in plan)
    print(f"Организация {args.org_id}: строк {len(plan)}, изменений {pending}, пропусков {len(plan) - pending}")
    for uid, desired, state in plan:
        if state == "pending":
            print(f"  UID {uid} -> {desired}")
    if args.dry_run or not pending:
        print("Проверка завершена. PATCH не отправлялись.")
        return 0
    expected = f"APPLY {args.org_id} {pending}"
    if input(f"Для применения введите {expected}: ").strip() != expected:
        print("Отменено. PATCH не отправлялись.")
        return 0
    report = ROOT / (path.stem + ".results_" + uuid4().hex[:8] + ".jsonl")
    print(f"Отчёт: {report}")
    return execute(client, plan, report)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nОстановлено. Если PATCH уже начались, проверьте отчёт перед повторным запуском.", file=sys.stderr)
        sys.exit(130)
    except (UserError, OSError, ValueError, csv.Error) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        sys.exit(1)
