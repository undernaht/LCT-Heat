"""Схемы ответов API.

Наружу всё уходит в EPSG:4326 — перепроецирование живёт только здесь и в ingest.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from ..compliance.report import ComplianceReport
from ..economics import fee as fee_module
from ..economics.estimate import ApprovalRisk, CostBreakdown
from ..geo import crs
from ..routing.alternatives import VariantSet, variant_id_of
from ..routing.group import GroupSolution
from ..routing.manual import ManualEvaluation
from ..routing.solver import RouteSolution, TapCandidate


def geojson_of(geom: BaseGeometry, metric_crs: str) -> dict[str, Any]:
    return mapping(crs.to_wgs84(geom, metric_crs))


class AreaSummary(BaseModel):
    name: str
    crs: str
    bbox: list[float] = Field(
        description="[minlon, minlat, maxlon, maxlat] — порядок RFC 7946"
    )
    counts: dict[str, int]
    perspective: list[dict[str, Any]] = Field(default_factory=list)
    normatives_verified: bool = False
    import_report: dict[str, Any] | None = Field(
        default=None,
        description="что прочитано, что отброшено и почему",
    )


class UploadResult(BaseModel):
    name: str
    recognised: dict[str, int] = Field(
        description="опознанные слои и размер исходного файла в байтах"
    )
    unrecognised: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(description="сколько объектов дошло до модели")
    crs: str
    import_report: dict[str, Any] = Field(default_factory=dict)


class BuildingBrief(BaseModel):
    id: str
    name: str | None = None
    use: str
    floors: int | None = None
    is_perspective: bool
    q_total_gcal_h: float
    footprint_m2: float
    centroid: list[float]


class HydraulicsOut(BaseModel):
    q_gcal_h: float
    schedule: str
    g_t_h: float
    du_mm: int
    v_m_s: float
    r_pa_m: float
    dp_bar: float
    head_m: float
    head_available_m: float | None
    # Ключевой инженерный аргумент — «из 53 м ушло 9 на трассу и 10 на ИТП,
    # остаток 34» — существовал только в консоли и не доходил до сервиса.
    head_required_at_consumer_m: float
    head_reserve_m: float | None
    head_known: bool
    head_verdict: str
    head_ok: bool
    explanation: list[str]


class CostRow(BaseModel):
    title: str
    value_rub: float


class CrossingOut(BaseModel):
    kind: str
    target: str
    length_m: float
    cost_rub: float


class CostOut(BaseModel):
    total_rub: float
    rub_per_m: float
    rows: list[CostRow]
    crossings: list[CrossingOut]
    length_by_surface_m: dict[str, float]
    chambers_count: int
    search_penalty_rub: float


class RiskOut(BaseModel):
    score: float
    foreign_parcels: int
    roadway_opening_m: float
    protected_zones: int
    closed_crossings: int
    needs_reconstruction: bool


class CheckOut(BaseModel):
    where: str
    kind: str
    rule_id: str
    title: str
    clause: str
    target_id: str
    required_m: float
    actual_m: float
    status: str
    note: str | None = None


class ComplianceOut(BaseModel):
    status: str
    checks_total: int
    failures: int
    conditionals: int
    checks: list[CheckOut]


class CandidateOut(BaseModel):
    edge_id: str
    node_name: str | None
    du_mm: int
    total_rub: float
    route_rub: float
    tap_rub: float
    reconstruction_rub: float
    reserve_gcal_h: float
    head_available_m: float | None
    point: list[float]
    # Объяснение «почему не эта врезка» писалось в солвере и обрывалось здесь:
    # пользователь видел двенадцать точек с ценами и без единого вердикта.
    accepted: bool = True
    rejected_reason: str | None = None


class FeeLineOut(BaseModel):
    title: str
    quantity: float
    unit: str
    rate_rub: float
    amount_rub: float


class FeeOut(BaseModel):
    method: str
    method_title: str
    lines: list[FeeLineOut]
    total_rub: float
    vat_rub: float
    total_with_vat_rub: float
    basis: str
    note: str
    is_estimated: bool


class VariantOut(BaseModel):
    id: str
    profile: str
    profile_title: str
    is_pareto: bool
    geometry: dict[str, Any]
    length_m: float
    du_mm: int
    laying: str
    tap_point: list[float]
    tap_name: str | None
    hydraulics: HydraulicsOut
    cost: CostOut
    fee: FeeOut
    risk: RiskOut
    compliance: ComplianceOut
    vertical: ProfileOut
    candidates: list[CandidateOut]
    field_notes: dict[str, Any]


class ProfilePointOut(BaseModel):
    d: float          # пикет, м
    ground: float     # отметка земли
    top: float        # отметка верха трубы
    depth: float      # глубина заложения


class SpecialPointOut(BaseModel):
    kind: str         # air | drain
    distance_m: float
    ground_m: float
    depth_m: float


class ProfileCheckOut(BaseModel):
    name: str
    ok: bool
    value: float
    limit: float
    unit: str
    detail: str


class ProfileOut(BaseModel):
    """Продольный профиль. available=false — рельеф для района не загружен."""

    available: bool
    ok: bool = True
    points: list[ProfilePointOut] = []
    special: list[SpecialPointOut] = []
    checks: list[ProfileCheckOut] = []
    ground_min_m: float = 0.0
    ground_max_m: float = 0.0
    geodetic_rise_m: float = 0.0
    depth_min_m: float = 0.0
    depth_max_m: float = 0.0
    depth_mean_m: float = 0.0
    excavation_m3: float = 0.0
    air_points: int = 0
    drain_points: int = 0
    steepest_ground_slope: float = 0.0


class GeometryIssueOut(BaseModel):
    kind: str
    detail: str
    at_m: float | None = None


class ManualEvalOut(BaseModel):
    geometry: dict[str, Any]
    length_m: float
    du_mm: int
    laying: str
    admissible: bool
    hydraulics: HydraulicsOut
    cost: CostOut
    fee_total_with_vat_rub: float
    fee_method: str
    risk: RiskOut
    compliance: ComplianceOut
    issues: list[GeometryIssueOut] = Field(default_factory=list)
    tap_edge_id: str | None = None
    tap_node_name: str | None = None
    tap_distance_m: float | None = None
    # Сравнение с расчётной трассой — сколько стоит ручное решение
    reference_length_m: float | None = None
    reference_cost_rub: float | None = None
    reference_risk: float | None = None
    cost_delta_rub: float | None = None
    cost_delta_share: float | None = None
    elapsed_s: float = 0.0


class ManualRouteIn(BaseModel):
    """Трасса, нарисованная в интерфейсе. Координаты в EPSG:4326."""

    building_id: str
    coordinates: list[tuple[float, float]] = Field(
        min_length=2, max_length=2000,
        description="[[lon, lat], …] — ломаная от здания до точки врезки",
    )
    laying: str = "channel"
    resolution: float = Field(1.0, ge=0.5, le=5.0)
    snap: bool = Field(
        True,
        description="подтянуть концы к зданию и к существующей сети",
    )
    compare: bool = Field(True, description="сравнить с расчётной трассой")


class GroupSegmentOut(BaseModel):
    id: str
    geometry: dict[str, Any]
    serves: list[str]
    load_gcal_h: float
    du_mm: int
    length_m: float
    is_shared: bool


class GroupConnectionOut(BaseModel):
    building_id: str
    load_gcal_h: float
    tap_kind: str
    tap_target: str
    geometry: dict[str, Any]


class GroupSolveOut(BaseModel):
    profile: str
    laying: str
    schedule: str
    segments: list[GroupSegmentOut]
    connections: list[GroupConnectionOut]
    total_length_m: float
    total_cost_rub: float
    fee_total_with_vat_rub: float
    independent_length_m: float
    independent_cost_rub: float
    independent_fee_rub: float
    saving_rub: float
    saving_share: float
    length_saving_m: float
    fee_saving_rub: float
    compliance_status: str
    compliance_failures: int
    compliance_conditionals: int
    notes: dict[str, Any]
    elapsed_s: float


class SolveOut(BaseModel):
    building_id: str
    variants: list[VariantOut]
    pareto_ids: list[str]
    elapsed_s: float


PROFILE_TITLES = {
    "min_cost": "Минимальная стоимость",
    "min_approvals": "Минимум согласований",
    "reliable": "Надёжность и резерв",
}


def _cost_out(cost: CostBreakdown, length_m: float) -> CostOut:
    return CostOut(
        total_rub=round(cost.total_rub),
        rub_per_m=round(cost.total_rub / max(length_m, 1.0)),
        rows=[CostRow(title=title, value_rub=round(value)) for title, value in cost.as_rows()],
        crossings=[
            CrossingOut(kind=c.kind, target=c.target, length_m=c.length_m,
                        cost_rub=round(c.cost_rub))
            for c in cost.crossings
        ],
        length_by_surface_m=cost.length_by_surface_m,
        chambers_count=cost.chambers_count,
        search_penalty_rub=round(cost.search_penalty_rub),
    )


def _risk_out(risk: ApprovalRisk) -> RiskOut:
    return RiskOut(
        score=risk.score,
        foreign_parcels=risk.foreign_parcels,
        roadway_opening_m=risk.roadway_opening_m,
        protected_zones=risk.protected_zones,
        closed_crossings=risk.closed_crossings,
        needs_reconstruction=risk.needs_reconstruction,
    )


def _compliance_out(protocol: ComplianceReport) -> ComplianceOut:
    return ComplianceOut(
        status=protocol.status.value,
        checks_total=protocol.checks_total,
        failures=len(protocol.failures),
        conditionals=len(protocol.conditionals),
        checks=[
            CheckOut(
                where=c.where, kind=c.kind, rule_id=c.rule_id, title=c.title,
                clause=c.clause, target_id=c.target_id, required_m=c.required_m,
                actual_m=c.actual_m, status=c.status.value, note=c.note,
            )
            for c in protocol.checks
        ],
    )


def _candidate_out(candidate: TapCandidate, metric_crs: str) -> CandidateOut:
    point = crs.to_wgs84(candidate.point, metric_crs)
    return CandidateOut(
        edge_id=candidate.edge_id,
        node_name=candidate.node_name,
        du_mm=candidate.du_mm,
        total_rub=round(candidate.total_rub),
        route_rub=round(candidate.route_rub),
        tap_rub=round(candidate.tap_rub),
        reconstruction_rub=round(candidate.reconstruction_rub),
        reserve_gcal_h=round(candidate.reserve_gcal_h, 3),
        head_available_m=candidate.head_available_m,
        point=[point.x, point.y],
        accepted=candidate.accepted,
        rejected_reason=candidate.rejected_reason,
    )


def _fee_out(solution: RouteSolution) -> FeeOut:
    fee = fee_module.calculate(
        solution.q_gcal_h, {solution.du_mm: solution.length_m}, solution.laying
    )
    return FeeOut(
        method=fee.method,
        method_title=fee.method_title,
        lines=[
            FeeLineOut(
                title=line.title, quantity=line.quantity, unit=line.unit,
                rate_rub=round(line.rate_rub), amount_rub=round(line.amount_rub),
            )
            for line in fee.lines
        ],
        total_rub=round(fee.total_rub),
        vat_rub=round(fee.vat_rub),
        total_with_vat_rub=round(fee.total_with_vat_rub),
        basis=fee.basis,
        note=fee.note,
        is_estimated=fee.is_estimated,
    )


def _profile_out(profile) -> ProfileOut:
    if profile is None or not profile.available:
        return ProfileOut(available=False)
    return ProfileOut(
        available=True,
        ok=profile.ok,
        points=[
            ProfilePointOut(d=p.distance_m, ground=p.ground_m, top=p.pipe_top_m, depth=p.depth_m)
            for p in profile.points
        ],
        special=[
            SpecialPointOut(
                kind=s.kind, distance_m=s.distance_m, ground_m=s.ground_m, depth_m=s.depth_m
            )
            for s in profile.special
        ],
        checks=[
            ProfileCheckOut(
                name=c.name, ok=c.ok, value=c.value, limit=c.limit, unit=c.unit, detail=c.detail
            )
            for c in profile.checks
        ],
        ground_min_m=profile.ground_min_m,
        ground_max_m=profile.ground_max_m,
        geodetic_rise_m=profile.geodetic_rise_m,
        depth_min_m=profile.depth_min_m,
        depth_max_m=profile.depth_max_m,
        depth_mean_m=profile.depth_mean_m,
        excavation_m3=profile.excavation_m3,
        air_points=profile.air_points,
        drain_points=profile.drain_points,
        steepest_ground_slope=profile.steepest_ground_slope,
    )


def variant_out(solution: RouteSolution, metric_crs: str, *, is_pareto: bool) -> VariantOut:
    tap = crs.to_wgs84(solution.connection.point, metric_crs)
    h = solution.hydraulics

    return VariantOut(
        id=variant_id_of(solution),
        profile=solution.profile,
        profile_title=PROFILE_TITLES.get(solution.profile, solution.profile),
        is_pareto=is_pareto,
        geometry=geojson_of(solution.geometry, metric_crs),
        length_m=round(solution.length_m, 1),
        du_mm=solution.du_mm,
        laying=solution.laying.value,
        tap_point=[tap.x, tap.y],
        tap_name=solution.connection.node_name,
        hydraulics=HydraulicsOut(
            q_gcal_h=round(h.q_gcal_h, 4), schedule=h.schedule, g_t_h=round(h.g_t_h, 2),
            du_mm=h.du_mm, v_m_s=round(h.v_m_s, 2), r_pa_m=round(h.r_pa_m, 1),
            dp_bar=round(h.dp_bar, 3), head_m=round(h.head_m, 2),
            head_available_m=h.head_available_m,
            head_required_at_consumer_m=h.head_required_at_consumer_m,
            head_reserve_m=h.head_reserve_m,
            head_known=h.head_known,
            head_verdict=h.head_verdict,
            head_ok=h.head_ok,
            explanation=solution.diameter_explanation,
        ),
        cost=_cost_out(solution.cost, solution.length_m),
        fee=_fee_out(solution),
        risk=_risk_out(solution.risk),
        compliance=_compliance_out(solution.compliance),
        vertical=_profile_out(solution.vertical),
        candidates=[_candidate_out(c, metric_crs) for c in solution.candidates[:12]],
        field_notes=solution.field_notes,
    )


def manual_out(
    result: ManualEvaluation, geometry, metric_crs: str, elapsed_s: float
) -> ManualEvalOut:
    h = result.hydraulics
    return ManualEvalOut(
        geometry=geojson_of(geometry, metric_crs),
        length_m=result.length_m,
        du_mm=result.du_mm,
        laying=result.laying.value,
        admissible=result.admissible,
        hydraulics=HydraulicsOut(
            q_gcal_h=round(h.q_gcal_h, 4), schedule=h.schedule, g_t_h=round(h.g_t_h, 2),
            du_mm=h.du_mm, v_m_s=round(h.v_m_s, 2), r_pa_m=round(h.r_pa_m, 1),
            dp_bar=round(h.dp_bar, 3), head_m=round(h.head_m, 2),
            head_available_m=h.head_available_m,
            head_required_at_consumer_m=h.head_required_at_consumer_m,
            head_reserve_m=h.head_reserve_m, head_known=h.head_known,
            head_verdict=h.head_verdict, head_ok=h.head_ok, explanation=[],
        ),
        cost=_cost_out(result.cost, result.length_m),
        fee_total_with_vat_rub=round(result.fee_total_with_vat_rub),
        fee_method=result.fee_method,
        risk=_risk_out(result.risk),
        compliance=_compliance_out(result.protocol),
        issues=[
            GeometryIssueOut(kind=issue.kind, detail=issue.detail, at_m=issue.at_m)
            for issue in result.issues
        ],
        tap_edge_id=result.tap_edge_id,
        tap_node_name=result.tap_node_name,
        tap_distance_m=result.tap_distance_m,
        reference_length_m=result.reference_length_m,
        reference_cost_rub=(
            round(result.reference_cost_rub) if result.reference_cost_rub else None
        ),
        reference_risk=result.reference_risk,
        cost_delta_rub=(
            round(result.cost_delta_rub) if result.cost_delta_rub is not None else None
        ),
        cost_delta_share=(
            round(result.cost_delta_share, 4)
            if result.cost_delta_share is not None else None
        ),
        elapsed_s=round(elapsed_s, 2),
    )


def group_out(result: GroupSolution, metric_crs: str, elapsed_s: float) -> GroupSolveOut:
    return GroupSolveOut(
        profile=result.profile,
        laying=result.laying.value,
        schedule=result.schedule,
        segments=[
            GroupSegmentOut(
                id=segment.id,
                geometry=geojson_of(segment.geometry, metric_crs),
                serves=segment.serves,
                load_gcal_h=segment.load_gcal_h,
                du_mm=segment.du_mm,
                length_m=segment.length_m,
                is_shared=segment.is_shared,
            )
            for segment in result.segments
        ],
        connections=[
            GroupConnectionOut(
                building_id=connection.building_id,
                load_gcal_h=round(connection.load_gcal_h, 4),
                tap_kind=connection.tap_kind,
                tap_target=connection.tap_target,
                geometry=geojson_of(connection.path, metric_crs),
            )
            for connection in result.connections
        ],
        total_length_m=result.total_length_m,
        total_cost_rub=round(result.total_cost_rub),
        fee_total_with_vat_rub=round(result.fee_total_with_vat_rub),
        independent_length_m=result.independent_length_m,
        independent_cost_rub=round(result.independent_cost_rub),
        independent_fee_rub=round(result.independent_fee_rub),
        saving_rub=round(result.saving_rub),
        saving_share=round(result.saving_share, 4),
        length_saving_m=round(result.length_saving_m, 1),
        fee_saving_rub=round(result.fee_saving_rub),
        compliance_status=result.compliance_status,
        compliance_failures=result.compliance_failures,
        compliance_conditionals=result.compliance_conditionals,
        notes=result.notes,
        elapsed_s=round(elapsed_s, 2),
    )


def solve_out(result: VariantSet, metric_crs: str, elapsed_s: float) -> SolveOut:
    return SolveOut(
        building_id=result.building_id,
        variants=[
            variant_out(s, metric_crs, is_pareto=variant_id_of(s) in result.pareto_ids)
            for s in result.variants
        ],
        pareto_ids=result.pareto_ids,
        elapsed_s=round(elapsed_s, 2),
    )
