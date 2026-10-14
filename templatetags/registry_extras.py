from django import template

register = template.Library()


@register.filter
def mask(value, keep="3,2"):
    """إخفاء جزئي لرقم هوية/جوال: 400123456 → 400••••56."""
    s = str(value or "")
    try:
        a, b = (int(x) for x in str(keep).split(","))
    except ValueError:
        a, b = 3, 2
    if len(s) <= a + b:
        return s
    return s[:a] + "•" * (len(s) - a - b) + s[len(s) - b:]
