"""Policy for newly chosen passwords only; never normalize or log passwords."""
import re

def password_errors(value):
    if not isinstance(value, str):
        return ["Введите пароль."]
    checks = (
        (8 <= len(value) <= 64, "Пароль должен содержать от 8 до 64 символов."),
        (re.search(r"[A-Z]", value), "Добавьте хотя бы одну заглавную латинскую букву (A–Z)."),
        (re.search(r"[a-z]", value), "Добавьте хотя бы одну строчную латинскую букву (a–z)."),
        (re.search(r"[0-9]", value), "Добавьте хотя бы одну цифру (0–9)."),
        (any(33 <= ord(c) <= 126 and not c.isalnum() for c in value), "Добавьте хотя бы один специальный символ."),
        (bool(re.fullmatch(r"[!-~]+", value)), "Допустимы только латинские буквы A–Z, a–z, цифры и стандартные специальные символы ASCII."),
        (not any(c.isspace() for c in value), "Пробелы и другие пробельные символы запрещены."),
    )
    return [message for valid, message in checks if not valid]
