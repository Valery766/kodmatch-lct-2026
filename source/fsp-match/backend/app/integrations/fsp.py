def snapshot(profile_data, is_demo):
    if is_demo:
        spec = profile_data.get("specialization", "backend_python")
        return {"status": "synthetic_demo", "source": "КОД.МЭТЧ: синтетический пример, API ФСП не подключён", "verified": False,
                "achievements": [{"id": "demo-" + spec, "title": "Учебное соревнование «Практика разработки»", "specialization": spec,
                                  "level": "finalist", "year": 2026, "synthetic": True, "verified": True, "source": "demo_registry"}]}
    if profile_data.get("fsp_id") or profile_data.get("fsp_link"):
        return {"status": "unverified", "source": "ID указан пользователем; внешний API ФСП не предоставлен", "verified": False, "achievements": []}
    return {"status": "not_linked", "source": "Профиль ФСП не указан. Все основные функции доступны.", "verified": False, "achievements": []}
