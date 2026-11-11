"""Secret Santa pairing; independent of Telegram and storage."""
import random


def make_pairs(ids):
    ids = list(ids)
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise ValueError("Нужны минимум 2 разных участника.")
    for _ in range(1000):
        shuffled = ids[:]
        random.shuffle(shuffled)
        if all(giver != receiver for giver, receiver in zip(ids, shuffled)):
            return list(zip(ids, shuffled))
    raise ValueError("Не удалось создать назначения. Подтверди жеребьёвку заново.")
