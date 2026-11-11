"""Input rules shared by handlers and offline tests."""
import re


def validate_title(value: str) -> str:
    title = value.strip()
    if not 1 <= len(title) <= 100:
        raise ValueError("Название должно содержать от 1 до 100 символов.")
    return title


def parse_budget(value: str) -> int:
    value = value.strip()
    if not re.fullmatch(r"[0-9]+(?:[.,][0-9]{1,2})?", value) or len(value) > 32:
        raise ValueError("Введите сумму в RON, например 100 или 100,50; максимум 2 десятичных знака.")
    whole, _, fraction = value.replace(",", ".").partition(".")
    bani = int(whole) * 100 + int(fraction.ljust(2, "0"))
    if not 1 <= bani <= 10_000_000:
        raise ValueError("Бюджет должен быть больше 0 и не больше 100 000 RON.")
    return bani


def format_budget(bani: int) -> str:
    return f"{bani // 100},{bani % 100:02d} RON"
