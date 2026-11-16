"""Enable the go_gigi Post Studio wall connection from its Termux checkout."""

import argparse
import getpass
import json
import os
from pathlib import Path
import tempfile

from kyntrion_tools.vk_api import VkApi, VkApiError


def check_owner(token, group, owner):
    api = VkApi(token, group)
    users = api.call("users.get")
    if not users or users[0].get("id") != owner:
        raise ValueError("Ключ должен принадлежать владельцу из настроек PiarBot.")
    if not int(api.call("account.getAppPermissions")) & 8192:
        raise ValueError("У ключа отсутствует разрешение wall.")
    result = api.call("groups.getById", group_ids=group, fields="admin_level,is_admin")
    groups = result.get("groups", []) if isinstance(result, dict) else result
    if not groups or groups[0].get("id") != group or groups[0].get("admin_level", 0) < 2:
        raise ValueError("Владелец должен быть редактором или администратором go_gigi.")


def save_token(path, token):
    if not token or any(ch.isspace() for ch in token):
        raise ValueError("Проверьте формат ключа.")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if line.split("=", 1)[0].strip() != "VK_WALL_TOKEN"]
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w",
                                     encoding="utf-8") as tmp:
        try:
            os.chmod(tmp.name, 0o600)
            tmp.write("\n".join([*lines, "VK_WALL_TOKEN=" + token, ""]))
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            os.unlink(tmp.name)
            raise
    try:
        os.replace(tmp.name, path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nexshell", action="store_true",
                        help="Использовать локальный конфиг vk-post для этой группы")
    parser.add_argument("--check", action="store_true", help="Только проверить установленный ключ")
    args = parser.parse_args()
    import config
    import db
    group = config.VK_GROUP_ID
    if group != 157735873:
        raise ValueError("Этот инструмент настраивает стену go_gigi.")
    owner = int(db.get_config("owner_id", "0"))
    if args.nexshell:
        cfg = json.loads((Path.home() / ".config/ai-tasker/vk.json").read_text())
        if str(cfg.get("group_id", "")).lstrip("-") != str(group):
            raise ValueError("Конфигурация vk-post указывает на другую группу.")
        token = cfg.get("access_token", "")
    elif args.check:
        token = os.getenv("VK_WALL_TOKEN", "")
    else:
        token = getpass.getpass("Ключ владельца ВК с правом wall (скрытый ввод): ").strip()
    check_owner(token, group, owner)
    if not args.check:
        save_token(config.ENV_PATH, token)
        print("Ключ сохранён. Примените настройку командой ./run.sh restart.")
    else:
        print("Доступ к стене go_gigi подтверждён.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, VkApiError) as exc:
        raise SystemExit(str(exc)) from None
