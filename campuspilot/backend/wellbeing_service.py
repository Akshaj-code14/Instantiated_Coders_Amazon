
"""Basic student wellbeing support for CampusPilot."""

from datetime import datetime


def get_wellbeing_support(
    *,
    student_id: str,
    mood: str | None,
    risk_flag: bool,
) -> dict:
    """Return supportive guidance without attempting a diagnosis."""

    if not student_id.strip():
        raise ValueError("student_id cannot be empty.")

    if not isinstance(risk_flag, bool):
        raise ValueError("risk_flag must be a boolean.")

    normalized_mood = mood.strip().lower() if mood else None

    if risk_flag:
        message = (
            "Your wellbeing matters. Please reach out to a trusted person "
            "or a qualified mental health professional as soon as possible. "
            "If you may be in immediate danger, contact local emergency "
            "services or go to the nearest emergency department."
        )
        suggestions = [
            "Contact someone you trust and tell them how you are feeling.",
            "Seek professional support.",
            "If there is immediate danger, contact emergency services.",
        ]
        urgency = "high"

    elif normalized_mood in {"stressed", "overwhelmed", "anxious"}:
        message = (
            "It sounds like things may feel difficult right now. "
            "Consider taking a short break and choosing one manageable task."
        )
        suggestions = [
            "Take a few slow breaths and drink some water.",
            "Break your next task into a small, achievable step.",
            "Consider talking with a trusted friend or counsellor.",
        ]
        urgency = "moderate"

    elif normalized_mood in {"sad", "low", "down"}:
        message = (
            "I'm sorry you may be having a difficult day. "
            "You do not have to handle everything alone."
        )
        suggestions = [
            "Reach out to someone you trust.",
            "Take some time for a basic need, such as food, rest, or water.",
            "Consider speaking with a counsellor if these feelings persist.",
        ]
        urgency = "moderate"

    elif normalized_mood in {"happy", "good", "calm", "motivated"}:
        message = (
            "It's good to hear that you're feeling positive. "
            "Use that momentum at a pace that feels sustainable."
        )
        suggestions = [
            "Set one realistic goal for today.",
            "Take regular breaks.",
            "Make time for rest and activities you enjoy.",
        ]
        urgency = "low"

    else:
        message = (
            "Check in with yourself and choose one small step "
            "that would help you feel supported today."
        )
        suggestions = [
            "Pause and notice how you are feeling.",
            "Take a short break if you need one.",
            "Reach out to someone you trust if you would like support.",
        ]
        urgency = "low"

    return {
        "student_id": student_id,
        "mood": normalized_mood,
        "urgency": urgency,
        "message": message,
        "suggestions": suggestions,
        "generated_at": datetime.now().astimezone().isoformat(),
    }
