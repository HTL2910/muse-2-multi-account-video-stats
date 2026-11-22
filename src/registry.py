"""Đăng ký trung tâm cho tất cả các hướng giải quyết của Hội đồng Chuyên gia."""

APPROACH_REGISTRY = {}

def register_approach(code: str, title: str, handler=None):
    APPROACH_REGISTRY[code] = {"title": title, "handler": handler}
    return handler
