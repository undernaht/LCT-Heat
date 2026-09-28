"""Проект технических условий на подключение — PDF.

Главное здесь — таблица «протяжённость сетей по диаметрам и типу прокладки».
Именно она вместе с подключаемой нагрузкой определяет плату за подключение по
ФЗ-190 и ПП РФ № 787, то есть является прямым входом в тарифный расчёт. Всё
остальное в документе — обоснование этой таблицы.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..domain.models import Building
from ..economics import fee as fee_module
from ..routing.alternatives import variant_id_of
from ..routing.solver import RouteSolution

# Кириллица: встроенные шрифты reportlab её не поддерживают, нужен TTF.
FONT_CANDIDATES: list[tuple[str, str]] = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]

_registered: tuple[str, str] | None = None


def _fonts() -> tuple[str, str]:
    """Зарегистрировать кириллический шрифт. Возвращает (обычный, жирный)."""
    global _registered
    if _registered:
        return _registered

    for regular, bold in FONT_CANDIDATES:
        if not Path(regular).exists():
            continue
        pdfmetrics.registerFont(TTFont("Doc", regular))
        bold_name = "Doc"
        if Path(bold).exists():
            pdfmetrics.registerFont(TTFont("Doc-Bold", bold))
            bold_name = "Doc-Bold"
        _registered = ("Doc", bold_name)
        return _registered

    raise RuntimeError(
        "Не найден TTF-шрифт с кириллицей. Встроенные шрифты reportlab её не "
        "поддерживают — добавь путь в FONT_CANDIDATES."
    )


def _styles() -> dict[str, ParagraphStyle]:
    regular, bold = _fonts()
    base = getSampleStyleSheet()

    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontName=bold, fontSize=15, leading=19,
            spaceAfter=2 * mm, alignment=TA_LEFT,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontName=regular, fontSize=9,
            textColor=colors.HexColor("#78716c"), spaceAfter=6 * mm,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontName=bold, fontSize=11.5, leading=14,
            spaceBefore=5 * mm, spaceAfter=2 * mm,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontName=regular, fontSize=9, leading=12.5,
        ),
        "cell": ParagraphStyle(
            "cell", parent=base["Normal"], fontName=regular, fontSize=8, leading=10,
        ),
        "note": ParagraphStyle(
            "note", parent=base["Normal"], fontName=regular, fontSize=7.5, leading=10,
            textColor=colors.HexColor("#78716c"), spaceBefore=2 * mm,
        ),
        "warn": ParagraphStyle(
            "warn", parent=base["Normal"], fontName=regular, fontSize=8, leading=11,
            textColor=colors.HexColor("#b45309"), spaceBefore=2 * mm,
        ),
    }


def _table(rows: list[list], widths: list[float], *, header: bool = True) -> Table:
    regular, bold = _fonts()
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0)

    style = [
        ("FONTNAME", (0, 0), (-1, -1), regular),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#e7e5e4")),
    ]
    if header:
        style += [
            ("FONTNAME", (0, 0), (-1, 0), bold),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f5f5f4")),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#a8a29e")),
        ]
    table.setStyle(TableStyle(style))
    return table


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


STATUS_LABEL = {
    "pass": "выдержан",
    "conditional": "требуются мероприятия",
    "fail": "нарушен",
}


def export(
    solution: RouteSolution,
    building: Building,
    *,
    area_name: str,
    generated_on: date | None = None,
) -> bytes:
    styles = _styles()
    regular, bold = _fonts()
    today = generated_on or date.today()

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Технические условия — {building.id}",
        author="Сервис моделирования трассировки тепловых сетей",
    )

    story: list = []
    add = story.append
    content_width = A4[0] - 36 * mm

    # --- Шапка ---
    add(Paragraph("Проект технических условий на подключение<br/>к системе теплоснабжения", styles["title"]))
    add(Paragraph(
        f"Объект «{building.id}» · район «{area_name}» · вариант трассы "
        f"«{variant_id_of(solution)}» · {today.strftime('%d.%m.%Y')}",
        styles["subtitle"],
    ))

    # --- 1. Объект ---
    add(Paragraph("1. Подключаемый объект", styles["h2"]))
    add(_table(
        [
            ["Показатель", "Значение"],
            ["Идентификатор объекта", building.id],
            ["Этажность", str(building.floors or "—")],
            ["Площадь застройки", f"{building.footprint_m2:,.0f} м²".replace(",", " ")],
            ["Отопление и вентиляция",
             f"{building.load.heating + building.load.ventilation:.3f} Гкал/ч" if building.load else "—"],
            ["Горячее водоснабжение (макс. час)",
             f"{building.load.dhw_max:.3f} Гкал/ч" if building.load else "—"],
            ["Подключаемая тепловая нагрузка", f"{solution.q_gcal_h:.3f} Гкал/ч"],
            ["Температурный график", f"{solution.schedule} °C"],
        ],
        [content_width * 0.5, content_width * 0.5],
    ))

    # --- 2. Точка подключения ---
    connection = solution.connection
    add(Paragraph("2. Точка подключения", styles["h2"]))
    add(_table(
        [
            ["Показатель", "Значение"],
            ["Точка подключения", connection.node_name or connection.edge_id],
            ["Диаметр существующего трубопровода", f"Ду{connection.du_mm}"],
            ["Резерв пропускной способности", f"{connection.reserve_gcal_h:.3f} Гкал/ч"],
            ["Располагаемый напор в точке подключения",
             f"{connection.head_available_m:.1f} м вод. ст." if connection.head_available_m else "—"],
            ["Требуется реконструкция участка",
             "да" if connection.needs_reconstruction else "нет"],
            ["Рассмотрено вариантов точек подключения", str(len(solution.candidates))],
        ],
        [content_width * 0.5, content_width * 0.5],
    ))

    # --- 3. Протяжённость по диаметрам — ключевая таблица ---
    laying_title = "канальная" if solution.laying.value == "channel" else "бесканальная"
    add(Paragraph("3. Протяжённость проектируемых сетей", styles["h2"]))
    add(Paragraph(
        "Таблица является исходными данными для расчёта платы за подключение: "
        "плата дифференцируется по диаметрам и типу прокладки (ПП РФ № 787).",
        styles["body"],
    ))
    add(Spacer(1, 2 * mm))
    add(_table(
        [
            ["Диаметр", "Тип прокладки", "Протяжённость, м"],
            [f"Ду{solution.du_mm}", laying_title, f"{solution.length_m:.1f}"],
            ["Итого", "", f"{solution.length_m:.1f}"],
        ],
        [content_width * 0.25, content_width * 0.45, content_width * 0.3],
    ))

    add(Paragraph("Распределение трассы по типам покрытия", styles["h2"]))
    surface_rows = [["Покрытие", "Протяжённость, м"]]
    for surface, length in sorted(
        solution.cost.length_by_surface_m.items(), key=lambda kv: -kv[1]
    ):
        surface_rows.append([surface, f"{length:.0f}"])
    add(_table(surface_rows, [content_width * 0.6, content_width * 0.4]))

    # --- 4. Гидравлика ---
    h = solution.hydraulics
    add(Paragraph("4. Гидравлический расчёт", styles["h2"]))
    add(_table(
        [
            ["Показатель", "Значение", "Основание / ограничение"],
            ["Расчётный расход теплоносителя", f"{h.g_t_h:.2f} т/ч", "Q, температурный график"],
            ["Принятый диаметр", f"Ду{h.du_mm}", "наименьший из ряда, проходящий по потерям"],
            ["Скорость движения воды", f"{h.v_m_s:.2f} м/с", "не более 3,5 м/с"],
            ["Удельные потери давления на трение", f"{h.r_pa_m:.0f} Па/м",
             "kэ = 0,0005 м (СП 124.13330, п. 8.5)"],
            ["Потери напора по трассе", f"{h.dp_bar:.2f} бар ({h.head_m:.1f} м вод. ст.)",
             "подающий и обратный, с учётом местных сопротивлений"],
            # Третье состояние обязательно: подписной документ не может
            # утверждать достаточность напора при отсутствии исходных данных.
            ["Располагаемый напор в точке врезки",
             f"{h.head_available_m:.1f} м вод. ст." if h.head_known else "нет данных",
             h.head_verdict],
            ["Остаётся на ИТП после трассы",
             f"{h.head_reserve_m + h.head_required_at_consumer_m:.1f} м вод. ст."
             if h.head_reserve_m is not None else "—",
             f"требуется не менее {h.head_required_at_consumer_m:.0f} м"],
        ],
        [content_width * 0.34, content_width * 0.26, content_width * 0.40],
    ))

    # --- 5. Нормоконтроль ---
    protocol = solution.compliance
    add(Paragraph("5. Протокол нормоконтроля", styles["h2"]))
    add(Paragraph(
        f"Выполнено проверок: {protocol.checks_total}. Нарушений: "
        f"{len(protocol.failures)}. Требуют защитных мероприятий: "
        f"{len(protocol.conditionals)}. Общий статус: "
        f"<b>{STATUS_LABEL.get(protocol.status.value, protocol.status.value)}</b>.",
        styles["body"],
    ))
    add(Spacer(1, 2 * mm))

    if protocol.checks:
        rows = [["Участок", "Проверка", "Норма, м", "Факт, м", "Статус"]]
        for check in protocol.checks:
            note = f"<br/><font size=6 color='#b45309'>{check.note}</font>" if check.note else ""
            rows.append([
                Paragraph(check.where, styles["cell"]),
                Paragraph(
                    f"{check.title}<br/><font size=6 color='#78716c'>"
                    f"СП 124.13330.2012, {check.clause}</font>{note}",
                    styles["cell"],
                ),
                f"{check.required_m:.2f}",
                f"{check.actual_m:.2f}",
                STATUS_LABEL.get(check.status.value, check.status.value),
            ])
        add(_table(
            rows,
            [content_width * 0.13, content_width * 0.45, content_width * 0.12,
             content_width * 0.12, content_width * 0.18],
        ))
    else:
        add(Paragraph("Замечаний нет.", styles["body"]))

    add(PageBreak())

    # --- 6. Плата за подключение ---
    fee = fee_module.calculate(
        solution.q_gcal_h, {solution.du_mm: solution.length_m}, solution.laying
    )
    add(Paragraph("6. Плата за подключение", styles["h2"]))
    add(Paragraph(f"Основание: {fee.basis}. Способ определения: {fee.method_title}.",
                  styles["body"]))
    add(Paragraph(fee.note, styles["body"]))
    add(Spacer(1, 2 * mm))

    if fee.lines:
        rows = [["Составляющая", "Количество", "Ед.", "Ставка, ₽", "Сумма, ₽"]]
        for line in fee.lines:
            rows.append([
                Paragraph(line.title, styles["cell"]),
                f"{line.quantity:,.3f}".replace(",", " ").rstrip("0").rstrip("."),
                line.unit,
                _money(line.rate_rub),
                _money(line.amount_rub),
            ])
        rows.append(["Итого без НДС", "", "", "", _money(fee.total_rub)])
        rows.append([f"НДС {int(fee.vat_rub / max(fee.total_rub, 1) * 100)} %", "", "", "",
                     _money(fee.vat_rub)])
        rows.append(["Всего с НДС", "", "", "", _money(fee.total_with_vat_rub)])
        add(_table(
            rows,
            [content_width * 0.40, content_width * 0.15, content_width * 0.08,
             content_width * 0.18, content_width * 0.19],
        ))

    if fee.is_estimated:
        add(Paragraph(
            "⚠ Ставки платы за подключение приняты ориентировочными. Действующие "
            "утверждаются приказом органа регулирования субъекта Российской Федерации "
            "на соответствующий год и должны быть подставлены перед выдачей документа.",
            styles["warn"],
        ))

    # --- 7. Смета работ ---
    add(Paragraph("7. Ориентировочная стоимость строительно-монтажных работ", styles["h2"]))
    add(Paragraph(
        "Величина отличается от платы за подключение: смета — стоимость работ, "
        "плата — тариф, утверждённый регулятором.",
        styles["body"],
    ))
    add(Spacer(1, 2 * mm))
    rows = [["Статья затрат", "Сумма, ₽"]]
    for title, value in solution.cost.as_rows():
        rows.append([Paragraph(title, styles["cell"]), _money(value)])
    rows.append(["Итого", _money(solution.cost.total_rub)])
    rows.append([
        "Удельная стоимость",
        f"{solution.cost.total_rub / max(solution.length_m, 1):,.0f} ₽/м".replace(",", " "),
    ])
    add(_table(rows, [content_width * 0.7, content_width * 0.3]))

    if solution.cost.crossings:
        add(Paragraph("Пересечения преград", styles["h2"]))
        rows = [["Преграда", "Способ", "Длина, м", "Стоимость, ₽"]]
        for crossing in solution.cost.crossings:
            rows.append([
                Paragraph(crossing.target, styles["cell"]),
                crossing.kind,
                f"{crossing.length_m:.0f}" if crossing.length_m else "—",
                _money(crossing.cost_rub),
            ])
        add(_table(
            rows,
            [content_width * 0.42, content_width * 0.22, content_width * 0.16,
             content_width * 0.20],
        ))

    # --- 8. Оговорки ---
    add(KeepTogether([
        Paragraph("8. Оговорки", styles["h2"]),
        Paragraph(
            "Документ сформирован автоматически по результатам моделирования и "
            "<b>не является проектной документацией</b>. Трасса построена по "
            "цифровой модели местности; перед проектированием требуется "
            "инженерно-геодезическая съёмка и согласование с владельцами "
            "смежных коммуникаций.",
            styles["body"],
        ),
        Paragraph(
            "Нормативные расстояния приняты по СП 124.13330.2012 «Тепловые сети», "
            "Приложение А. Соответствие действующей редакции с изменениями подлежит "
            "проверке. Слои существующих инженерных коммуникаций в демонстрационном "
            "наборе смоделированы и не отражают фактическое положение сетей.",
            styles["note"],
        ),
    ]))

    document.build(story)
    return buffer.getvalue()
