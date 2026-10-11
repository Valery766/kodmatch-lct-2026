from io import BytesIO
from pathlib import Path
import html
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def font_name(settings):
    candidates = [settings.pdf_font_path, "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"]
    for path in candidates:
        if path and Path(path).is_file():
            if "FSPUnicode" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("FSPUnicode", path))
            return "FSPUnicode"
    raise RuntimeError("Unicode PDF font unavailable; configure PDF_FONT_PATH")


def render_profile(profile, settings):
    font = font_name(settings)
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=42, rightMargin=42, topMargin=42, bottomMargin=42, title="КОД.МЭТЧ – профиль специалиста", author="КОД.МЭТЧ")
    styles = getSampleStyleSheet()
    normal = ParagraphStyle("FSPNormal", fontName=font, fontSize=10, leading=15, textColor=colors.HexColor("#232033"), spaceAfter=8)
    title = ParagraphStyle("FSPTitle", parent=normal, fontSize=22, leading=28, textColor=colors.HexColor("#402FFF"), spaceAfter=18)
    def p(text, style=normal, normalize=True):
        rendered = str(text).replace("\u2014", "–") if normalize else str(text)
        return Paragraph(html.escape(rendered).replace("\n", "<br/>"), style)
    story = [p("КОД.МЭТЧ", title), p(profile.get("full_name") or profile.get("display_name") or "Специалист", title, normalize=False), p(profile.get("headline", ""))]
    spec = {"backend_python": "Backend / Python", "frontend_ts": "Frontend / TypeScript", "data_sql": "Data / SQL"}.get(profile.get("specialization"), "")
    grades = {"junior": "Junior", "middle": "Middle"}
    verified = grades.get(profile.get("verified_grade"), "не подтверждён")
    formats = {"remote": "Удалённо", "office": "Офис", "hybrid": "Гибрид", "any": "Любой"}
    experience = profile.get("experience_years")
    years = f"{experience:g} лет" if isinstance(experience, (int, float)) else "Не указан"
    salary = "Не указана"
    if profile.get("salary_min") or profile.get("salary_max"):
        salary = f"{profile.get('salary_min') or '…'} – {profile.get('salary_max') or '…'} руб. / месяц до вычета НДФЛ"
    rows = [
        [p("Специализация"), p(spec)],
        [p("Заявленный уровень"), p(grades.get(profile.get("declared_grade"), ""))],
        [p("Проверенный уровень"), p(verified)],
        [p("Подтверждённые навыки"), p(", ".join(profile.get("verified_skills", [])) or "Проверка ещё не пройдена")],
        [p("Заявленные навыки"), p(", ".join(profile.get("skills", [])))],
        [p("Профессиональные роли"), p(", ".join(profile.get("roles", [])) or "Не указаны")],
        [p("Стаж"), p(years)],
        [p("Soft skills"), p(", ".join(profile.get("soft_skills", [])) or "Не указаны")],
        [p("Формат / город"), p(formats.get(profile.get("work_format"), "") + " / " + profile.get("location", ""))],
        [p("Ожидаемая вилка"), p(salary)],
    ]
    table = Table(rows, colWidths=[150, 360], hAlign="LEFT")
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F3ECF7")), ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDC9E8"))]))
    story.extend([table, Spacer(1, 14), p("Навыки без подтверждения, роли, стаж и soft skills указаны самим кандидатом и не повышают проверенный уровень."), p("О специалисте", title), p(profile.get("bio", ""))])
    if profile.get("contacts"):
        story.extend([p("Контакты: разрешены владельцем", title)] + [p(f"{k}: {v}", normalize=False) for k, v in profile["contacts"].items() if v and k != "telegram"])
    else:
        story.append(p("Контакты скрыты. Доступ открывается только после согласия на приглашение или собственного отклика."))
    document.build(story)
    return buffer.getvalue()
