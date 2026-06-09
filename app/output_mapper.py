from __future__ import annotations

import json
import re
from typing import Any

from app.models import WorkflowSession


def parse_json_if_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def deep_find_keys(data: Any, keys: set[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if key in keys and key not in found:
                found[key] = value
            nested = deep_find_keys(value, keys)
            for nested_key, nested_value in nested.items():
                found.setdefault(nested_key, nested_value)
    elif isinstance(data, list):
        for item in data:
            nested = deep_find_keys(item, keys)
            for nested_key, nested_value in nested.items():
                found.setdefault(nested_key, nested_value)
    return found


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return None


def _extract_numeric(value: Any) -> int | float | None:
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    digits = re.sub(r"[^\d.]", "", value)
    if not digits:
        return None
    try:
        return float(digits) if "." in digits else int(digits)
    except ValueError:
        return None


def _flatten_locations(value: Any) -> list[str]:
    locations: list[str] = []
    for item in _as_list(value):
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned:
                locations.append(cleaned)
        elif isinstance(item, dict):
            for key in ("city", "name", "location", "label", "address", "area"):
                text = _clean_text(item.get(key))
                if text:
                    locations.append(text)
                    break
    deduped: list[str] = []
    seen: set[str] = set()
    for location in locations:
        lowered = location.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(location)
    return deduped


def _extract_city_from_text(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) >= 2:
        for part in reversed(parts[:-1]):
            lowered = part.lower()
            if lowered in {"india", "maharashtra"}:
                continue
            return part
    return None


def _extract_country_from_text(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        return None
    return parts[-1]


def _extract_primary_location(locations: list[str]) -> str | None:
    return locations[0] if locations else None


def _infer_country(parsed_plan: Any, locations: list[str], zones: list[dict[str, Any]]) -> str | None:
    lookup = deep_find_keys(parsed_plan, {"country", "country_name"})
    country = _clean_text(_first_non_empty(lookup.get("country"), lookup.get("country_name")))
    if country:
        return country
    for zone in zones:
        for key in ("country", "country_name"):
            zone_country = _clean_text(zone.get(key))
            if zone_country:
                return zone_country
    for location in locations:
        if "," in location:
            return location.split(",")[-1].strip()
    return None


def _normalize_geofence_zones(value: Any, fallback_locations: list[str]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, zone in enumerate(_as_list(value), start=1):
        if not isinstance(zone, dict):
            continue
        center = zone.get("center") if isinstance(zone.get("center"), dict) else {}
        center_address = center.get("address") if isinstance(center, dict) else None
        lat = _first_non_empty(
            zone.get("lat"),
            zone.get("latitude"),
            center.get("lat") if isinstance(center, dict) else None,
            center.get("latitude") if isinstance(center, dict) else None,
        )
        lng = _first_non_empty(
            zone.get("lng"),
            zone.get("lon"),
            zone.get("longitude"),
            center.get("lng") if isinstance(center, dict) else None,
            center.get("lon") if isinstance(center, dict) else None,
            center.get("longitude") if isinstance(center, dict) else None,
        )
        radius = _first_non_empty(zone.get("radius_km"), zone.get("radius"), zone.get("radius_miles"))
        city = _first_non_empty(
            zone.get("city"),
            _extract_city_from_text(center_address),
            _extract_primary_location(fallback_locations),
        )
        country = _first_non_empty(
            zone.get("country"),
            zone.get("country_name"),
            _extract_country_from_text(center_address),
        )
        normalized.append(
            {
                "zone_name": _first_non_empty(zone.get("zone_name"), zone.get("name"), f"Zone {index}"),
                "city": city,
                "country": country,
                "latitude": lat,
                "longitude": lng,
                "radius": radius,
                "type": _first_non_empty(zone.get("type"), zone.get("zone_type"), "geofence"),
            }
        )
    return normalized


def _derive_locations_from_zones(zones: list[dict[str, Any]]) -> list[str]:
    derived: list[str] = []
    seen: set[str] = set()
    for zone in zones:
        for value in (zone.get("city"), zone.get("zone_name")):
            text = _clean_text(value)
            if not text:
                continue
            lowered = text.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            derived.append(text)
    return derived


def _derive_duration(phases: Any, explicit_duration: Any) -> str | None:
    duration = _clean_text(explicit_duration)
    if duration:
        return duration
    if not isinstance(phases, list):
        return None
    phase_ranges = [phase.get("weeks") for phase in phases if isinstance(phase, dict) and phase.get("weeks")]
    if phase_ranges:
        return ", ".join(phase_ranges)
    return None


def _normalize_recommended_channels(channel_split: Any, recommended_channels: Any) -> list[str]:
    channels = _as_list(recommended_channels)
    if isinstance(channel_split, dict):
        channels.extend(channel_split.keys())
    normalized: list[str] = []
    seen: set[str] = set()
    for channel in channels:
        if not isinstance(channel, str):
            continue
        cleaned = channel.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(cleaned)
    return normalized


def _normalize_meta_placements(placements: Any, channel_split: Any) -> list[str]:
    normalized = _flatten_locations(placements)
    if normalized:
        return normalized
    if not isinstance(channel_split, dict):
        return []
    placement_mapping = {
        "meta": ["Facebook Feed", "Instagram Feed", "Instagram Stories"],
        "facebook": ["Facebook Feed", "Facebook Reels"],
        "instagram": ["Instagram Feed", "Instagram Stories", "Instagram Reels"],
    }
    derived: list[str] = []
    for channel in channel_split:
        for marker, defaults in placement_mapping.items():
            if marker in channel.lower():
                derived.extend(defaults)
    deduped: list[str] = []
    seen: set[str] = set()
    for placement in derived:
        lowered = placement.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(placement)
    return deduped


def _normalize_ad_sets(phases: Any, explicit_ad_sets: Any) -> list[dict[str, Any]]:
    if isinstance(explicit_ad_sets, list) and explicit_ad_sets:
        return [ad_set for ad_set in explicit_ad_sets if isinstance(ad_set, dict)]
    derived: list[dict[str, Any]] = []
    for phase in _as_list(phases):
        if not isinstance(phase, dict):
            continue
        tactics = phase.get("channel_tactics", {})
        meta_tactics = tactics.get("Meta") if isinstance(tactics, dict) else None
        derived.append(
            {
                "name": _first_non_empty(phase.get("name"), phase.get("phase"), "Meta Ad Set"),
                "phase": _first_non_empty(phase.get("name"), phase.get("phase")),
                "audience": phase.get("audience") or phase.get("target_audience"),
                "tactics": meta_tactics or phase.get("tactics"),
            }
        )
    return derived


def _normalize_ad_creatives(explicit_ad_creatives: Any, brand_name: str | None, objective: str | None) -> list[dict[str, Any]]:
    if isinstance(explicit_ad_creatives, list) and explicit_ad_creatives:
        normalized: list[dict[str, Any]] = []
        for creative in explicit_ad_creatives:
            if isinstance(creative, dict):
                normalized.append(creative)
            elif isinstance(creative, str):
                normalized.append({"format": creative, "brief": creative})
        if normalized:
            return normalized
    return [
        {
            "format": "Static Image",
            "brief": f"{brand_name or 'Brand'} creative focused on {objective or 'campaign goals'}",
        }
    ]


def _derive_daily_budget(total_budget: Any) -> int | float | None:
    numeric_budget = _extract_numeric(total_budget)
    if numeric_budget is None:
        return None
    daily_budget = numeric_budget / 30
    return round(daily_budget, 2) if isinstance(daily_budget, float) else daily_budget


def _prune_empty(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            pruned = _prune_empty(item)
            if pruned in (None, "", [], {}):
                continue
            cleaned[key] = pruned
        return cleaned
    if isinstance(value, list):
        cleaned_list = [_prune_empty(item) for item in value]
        return [item for item in cleaned_list if item not in (None, "", [], {})]
    return value


def extract_useful_content(output: Any) -> Any:
    if output is None:
        return None
    if isinstance(output, dict):
        for key in ("result", "content", "data", "message", "text"):
            if key in output:
                return parse_json_if_string(output[key])
    if isinstance(output, str):
        return parse_json_if_string(output)
    return output


def compact_atlas_output(atlas_output: Any) -> dict[str, Any]:
    parsed = parse_json_if_string(atlas_output)
    lookup = deep_find_keys(parsed, {"brand", "brand_name", "name", "locations", "target_locations"})
    client = parsed.get("client", {}) if isinstance(parsed, dict) else {}
    executive_summary = parsed.get("executive_summary", {}) if isinstance(parsed, dict) else {}
    scoring = parsed.get("scoring", {}) if isinstance(parsed, dict) else {}
    paid_media = parsed.get("paid_media", {}) if isinstance(parsed, dict) else {}
    geo_gap = parsed.get("geo_gap", {}) if isinstance(parsed, dict) else {}
    competitive_landscape = parsed.get("competitive_landscape", {}) if isinstance(parsed, dict) else {}
    action_plan = parsed.get("action_plan", {}) if isinstance(parsed, dict) else {}
    measurement_audit = parsed.get("measurement_audit", {}) if isinstance(parsed, dict) else {}
    client_name = _first_non_empty(
        client.get("name_extracted") if isinstance(client, dict) else None,
        lookup.get("brand_name"),
        lookup.get("name"),
    )
    top_priorities = []
    for item in _as_list(executive_summary.get("top_3_priorities") if isinstance(executive_summary, dict) else None):
        if not isinstance(item, dict):
            continue
        top_priorities.append(
            _prune_empty(
                {
                    "title": item.get("title"),
                    "why_it_matters": item.get("why_it_matters"),
                }
            )
        )
    scoring_categories = []
    for category in _as_list(scoring.get("categories") if isinstance(scoring, dict) else None):
        if not isinstance(category, dict):
            continue
        scoring_categories.append(
            _prune_empty(
                {
                    "name": category.get("name"),
                    "score": category.get("score"),
                    "rationale": category.get("rationale"),
                    "key_observations": category.get("key_observations"),
                }
            )
        )
    competitor_names = []
    for competitor in _as_list(
        competitive_landscape.get("competitors") if isinstance(competitive_landscape, dict) else None
    ):
        if isinstance(competitor, dict):
            name = _clean_text(competitor.get("name"))
            if name:
                competitor_names.append(name)
    raw_summary = {
        "brand_name": client_name,
        "industry": client.get("industry_inferred") if isinstance(client, dict) else None,
        "primary_geo": client.get("primary_geo") if isinstance(client, dict) else None,
        "business_model": client.get("business_model_inferred") if isinstance(client, dict) else None,
        "report_type": parsed.get("report_type") if isinstance(parsed, dict) else None,
        "tier": parsed.get("tier", {}).get("assigned") if isinstance(parsed, dict) and isinstance(parsed.get("tier"), dict) else None,
        "overall_score": scoring.get("overall_score") if isinstance(scoring, dict) else None,
        "grade": scoring.get("grade_label") if isinstance(scoring, dict) else None,
        "summary": executive_summary.get("snapshot") if isinstance(executive_summary, dict) else None,
        "headline": executive_summary.get("headline") if isinstance(executive_summary, dict) else None,
        "top_priorities": top_priorities,
        "strategic_gaps": scoring_categories,
        "paid_media_status": paid_media.get("ad_maturity_level") if isinstance(paid_media, dict) else None,
        "geo_insight": geo_gap.get("pin_code_analysis") if isinstance(geo_gap, dict) else None,
        "geofit_score": geo_gap.get("geofit_score") if isinstance(geo_gap, dict) else None,
        "competitive_summary": competitive_landscape.get("client_vs_competitors_summary")
        if isinstance(competitive_landscape, dict)
        else None,
        "competitors": competitor_names,
        "measurement_maturity": measurement_audit.get("first_party_data_maturity")
        if isinstance(measurement_audit, dict)
        else None,
        "recommended_actions": action_plan.get("tier1_14_day") if isinstance(action_plan, dict) else None,
        "locations": _flatten_locations(_first_non_empty(lookup.get("locations"), lookup.get("target_locations"), client.get("primary_geo") if isinstance(client, dict) else None)),
    }
    return _prune_empty(raw_summary)


def compact_audit_output(audit_output: Any) -> dict[str, Any]:
    parsed = parse_json_if_string(audit_output)
    lookup = deep_find_keys(
        parsed,
        {
            "audit_id",
            "summary",
            "findings",
            "recommendations",
            "issues",
            "strengths",
            "weaknesses",
            "seo",
            "performance",
            "technical_issues",
            "compliance",
            "local_seo",
            "conversion",
            "competitors_mentioned",
            "cities_mentioned",
            "priority_actions",
        },
    )
    return _prune_empty({
        "audit_id": lookup.get("audit_id"),
        "summary": lookup.get("summary"),
        "findings": _as_list(_first_non_empty(lookup.get("findings"), lookup.get("issues"))),
        "strengths": _as_list(lookup.get("strengths")),
        "weaknesses": _as_list(lookup.get("weaknesses")),
        "recommendations": _as_list(lookup.get("recommendations")),
        "priority_actions": _as_list(lookup.get("priority_actions")),
        "seo": lookup.get("seo"),
        "local_seo": lookup.get("local_seo"),
        "conversion": lookup.get("conversion"),
        "performance": lookup.get("performance"),
        "technical_issues": lookup.get("technical_issues"),
        "compliance": lookup.get("compliance"),
        "competitors_mentioned": _as_list(lookup.get("competitors_mentioned")),
        "cities_mentioned": _as_list(lookup.get("cities_mentioned")),
    })


def map_for_media_planner(session: WorkflowSession) -> dict[str, Any]:
    atlas = extract_useful_content(session.steps["atlas"].approved_output)
    audit = extract_useful_content(session.steps["audit"].approved_output)
    return {
        "url": str(session.url),
        "brand_intelligence": compact_atlas_output(atlas),
        "audit_findings": compact_audit_output(audit),
    }


def compact_media_plan_for_geo(media_plan: Any) -> dict[str, Any]:
    parsed = parse_json_if_string(media_plan)
    lookup = deep_find_keys(
        parsed,
        {
            "brand",
            "brand_name",
            "name",
            "goal",
            "objective",
            "campaign_objective",
            "primary_audience",
            "audience_segments",
            "target_locations",
            "locations",
            "geofence_zones",
            "channel_split",
            "recommended_channels",
            "total_monthly_budget",
            "budget",
            "phases",
            "duration",
            "country",
            "country_name",
        },
    )
    brand = lookup.get("brand") if isinstance(lookup.get("brand"), dict) else {}
    phases = lookup.get("phases")
    target_locations = _flatten_locations(lookup.get("target_locations") or lookup.get("locations"))
    zones = _normalize_geofence_zones(lookup.get("geofence_zones"), target_locations)
    if not target_locations:
        target_locations = _derive_locations_from_zones(zones)
    primary_location = _extract_primary_location(target_locations)
    country = _infer_country(parsed, target_locations, zones)
    return {
        "brand_name": brand.get("name") or lookup.get("brand_name") or lookup.get("name"),
        "primary_location": primary_location,
        "country": country,
        "target_locations": target_locations,
        "geofence_zones": zones,
        "audience_segments": _as_list(
            lookup.get("audience_segments") or brand.get("primary_audience") or lookup.get("primary_audience")
        ),
        "campaign_objective": lookup.get("campaign_objective") or brand.get("goal") or lookup.get("goal") or lookup.get("objective"),
        "budget": lookup.get("budget") or brand.get("total_monthly_budget") or lookup.get("total_monthly_budget"),
        "duration": _derive_duration(phases, lookup.get("duration")),
        "recommended_channels": _normalize_recommended_channels(
            lookup.get("channel_split"),
            lookup.get("recommended_channels"),
        ),
    }


def compact_media_plan_for_meta(media_plan: Any) -> dict[str, Any]:
    parsed = parse_json_if_string(media_plan)
    lookup = deep_find_keys(
        parsed,
        {
            "brand",
            "brand_name",
            "name",
            "goal",
            "objective",
            "campaign_objective",
            "primary_audience",
            "target_audience",
            "locations",
            "target_locations",
            "budget",
            "total_monthly_budget",
            "duration",
            "phases",
            "ad_sets",
            "ad_creatives",
            "placements",
            "channel_tactics",
            "channel_split",
            "country",
            "country_name",
        },
    )
    brand = lookup.get("brand") if isinstance(lookup.get("brand"), dict) else {}
    phases = lookup.get("phases")
    brand_name = brand.get("name") or lookup.get("brand_name") or lookup.get("name")
    objective = lookup.get("campaign_objective") or brand.get("goal") or lookup.get("goal") or lookup.get("objective")
    locations = _flatten_locations(lookup.get("locations") or lookup.get("target_locations"))
    if not locations:
        locations = _derive_locations_from_zones(_normalize_geofence_zones(lookup.get("geofence_zones"), []))
    country = _infer_country(parsed, locations, [])
    if country and locations and all("," not in location for location in locations):
        locations = [f"{location}, {country}" for location in locations]
    budget = lookup.get("budget") or brand.get("total_monthly_budget") or lookup.get("total_monthly_budget")
    return {
        "brand_name": brand_name,
        "campaign_name": f"{brand_name or 'Brand'} - {objective or 'Meta Campaign'}",
        "campaign_objective": objective,
        "target_audience": lookup.get("target_audience") or brand.get("primary_audience") or lookup.get("primary_audience"),
        "locations": locations,
        "budget": budget,
        "daily_budget": _derive_daily_budget(budget),
        "duration": _derive_duration(phases, lookup.get("duration")),
        "ad_sets": _normalize_ad_sets(phases, lookup.get("ad_sets")),
        "ad_creatives": _normalize_ad_creatives(lookup.get("ad_creatives"), brand_name, objective),
        "placements": _normalize_meta_placements(lookup.get("placements"), lookup.get("channel_split")),
        "special_ad_categories": [],
        "country": country,
    }


def map_for_geo_fence(session: WorkflowSession) -> dict[str, Any]:
    media_plan = extract_useful_content(session.steps["media_planner"].approved_output)
    return {
        "url": str(session.url),
        **compact_media_plan_for_geo(media_plan),
    }


def map_for_meta(session: WorkflowSession) -> dict[str, Any]:
    media_plan = extract_useful_content(session.steps["media_planner"].approved_output)
    return {
        "url": str(session.url),
        **compact_media_plan_for_meta(media_plan),
    }
