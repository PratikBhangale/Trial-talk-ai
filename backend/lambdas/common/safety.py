"""Layer 0: deterministic checks that run before any model call."""

import re
import unicodedata

# Emergency phrases in every supported language. Matched on accent-folded, lowercased text.
EMERGENCY_PHRASES = {
    "en": [
        "can't breathe", "cant breathe", "cannot breathe", "trouble breathing", "difficulty breathing",
        "chest pain", "throat closing", "face swelling", "lips swelling", "swollen lips", "swollen face",
        "passed out", "fainted", "unconscious", "seizure", "suicide", "kill myself", "overdose",
        "anaphylaxis", "stroke", "heart attack",
    ],
    "es": [
        "no puedo respirar", "dificultad para respirar", "dolor de pecho", "dolor en el pecho",
        "se me cierra la garganta", "hinchazon de la cara", "labios hinchados", "me desmaye",
        "inconsciente", "convulsion", "suicidio", "matarme", "sobredosis", "infarto", "derrame",
    ],
}

EMERGENCY_REPLY = {
    "en": ("This could be an emergency. Call 911 (or your local emergency number) now. "
           "We have alerted your study team."),
    "es": ("Esto podría ser una emergencia. Llame al 911 (o a su número de emergencias local) ahora. "
           "Hemos avisado a su equipo del estudio."),
}

STOP_WORDS = {"stop", "baja", "unsubscribe", "cancelar suscripcion"}


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def emergency_keywords(text: str) -> list[str]:
    folded = fold(text)
    return [p for phrases in EMERGENCY_PHRASES.values() for p in phrases if p in folded]


def is_stop(text: str) -> bool:
    return fold(text) in STOP_WORDS
