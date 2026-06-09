from app.models import WorkflowSession, WorkflowStep, WorkflowStatus
from app.output_mapper import (
    compact_atlas_output,
    compact_audit_output,
    extract_useful_content,
    map_for_media_planner,
    map_for_geo_fence,
    map_for_meta,
)


def build_session_with_media_plan_output(media_plan_output):
    return WorkflowSession(
        session_id="session-1",
        url="https://example.com",
        user_id="user_123",
        workflow_status=WorkflowStatus.RUNNING,
        steps={
            "atlas": WorkflowStep(session_id="session-1", step_id="atlas", status="APPROVED"),
            "audit": WorkflowStep(session_id="session-1", step_id="audit", status="APPROVED"),
            "media_planner": WorkflowStep(
                session_id="session-1",
                step_id="media_planner",
                status="APPROVED",
                approved_output=media_plan_output,
            ),
            "geo_fence": WorkflowStep(session_id="session-1", step_id="geo_fence", status="PENDING"),
            "meta": WorkflowStep(session_id="session-1", step_id="meta", status="PENDING"),
        },
    )


def test_json_string_media_planner_output_is_parsed():
    output = {
        "content": '{"brand":{"name":"Citimedia"},"geofence_zones":[{"zone_name":"Ameerpet"}]}'
    }
    parsed = extract_useful_content(output)

    assert parsed["brand"]["name"] == "Citimedia"
    assert parsed["geofence_zones"][0]["zone_name"] == "Ameerpet"


def test_atlas_output_is_compacted_for_media_planner():
    compacted = compact_atlas_output(
        {
            "client": {
                "name_extracted": "Citimedia",
                "industry_inferred": "Advertising Technology & Media Buying Marketplace",
                "primary_geo": "India",
                "business_model_inferred": "B2B Media Marketplace (Lead Generation)",
            },
            "report_type": "Catchment Intelligence Report",
            "tier": {"assigned": "Tier1"},
            "executive_summary": {
                "headline": "Dormant digital presence",
                "snapshot": "Citimedia is invisible to its target audience.",
                "top_3_priorities": [
                    {"title": "Activate Paid Media", "why_it_matters": "Need traffic"},
                    {"title": "Modernize Website", "why_it_matters": "Improve trust"},
                ],
            },
            "scoring": {
                "overall_score": 50,
                "grade_label": "Critical Gaps",
                "categories": [
                    {
                        "name": "Brand Voice & Messaging",
                        "score": 65,
                        "rationale": "Message is diluted",
                        "key_observations": ["Too many media types"],
                    }
                ],
            },
            "paid_media": {"ad_maturity_level": "Not Advertising"},
            "geo_gap": {"pin_code_analysis": "Big geo opportunity", "geofit_score": 30},
            "competitive_landscape": {
                "client_vs_competitors_summary": "Lagging behind rivals",
                "competitors": [{"name": "The Media Ant"}, {"name": "Lemma"}],
            },
            "measurement_audit": {"first_party_data_maturity": "low"},
            "action_plan": {"tier1_14_day": ["Launch search campaign"]},
        }
    )

    assert compacted["brand_name"] == "Citimedia"
    assert compacted["industry"] == "Advertising Technology & Media Buying Marketplace"
    assert compacted["summary"] == "Citimedia is invisible to its target audience."
    assert compacted["headline"] == "Dormant digital presence"
    assert compacted["top_priorities"][0]["title"] == "Activate Paid Media"
    assert compacted["strategic_gaps"][0]["name"] == "Brand Voice & Messaging"
    assert compacted["competitors"] == ["The Media Ant", "Lemma"]
    assert compacted["recommended_actions"] == ["Launch search campaign"]
    assert "positioning" not in compacted


def test_audit_output_is_compacted_for_media_planner():
    compacted = compact_audit_output(
        {
            "result": {
                "audit_id": "AUD-1",
                "summary": "SEO and conversion gaps found",
                "findings": ["Weak local SEO"],
                "recommendations": ["Add city landing pages"],
                "technical_issues": ["Missing schema"],
                "cities_mentioned": ["Hyderabad"],
                "raw": {"logs": ["secret"]},
            }
        }
    )

    assert compacted["audit_id"] == "AUD-1"
    assert compacted["summary"] == "SEO and conversion gaps found"
    assert compacted["findings"] == ["Weak local SEO"]
    assert compacted["recommendations"] == ["Add city landing pages"]
    assert compacted["technical_issues"] == ["Missing schema"]
    assert compacted["cities_mentioned"] == ["Hyderabad"]
    assert "raw" not in compacted


def test_media_planner_receives_compact_atlas_and_audit_fields_only():
    session = WorkflowSession(
        session_id="session-1",
        url="https://example.com",
        user_id="user_123",
        workflow_status=WorkflowStatus.RUNNING,
        steps={
            "atlas": WorkflowStep(
                session_id="session-1",
                step_id="atlas",
                status="APPROVED",
                approved_output={
                    "content": {
                        "brand": {"name": "Citimedia", "primary_audience": "B2B"},
                        "summary": "Brand summary",
                        "services": ["Billboards"],
                        "raw": {"logs": ["atlas secret"]},
                    }
                },
            ),
            "audit": WorkflowStep(
                session_id="session-1",
                step_id="audit",
                status="APPROVED",
                approved_output={
                    "content": {
                        "summary": "Audit summary",
                        "recommendations": ["Improve SEO"],
                        "technical_issues": ["Slow pages"],
                        "raw": {"logs": ["audit secret"]},
                    }
                },
            ),
            "media_planner": WorkflowStep(session_id="session-1", step_id="media_planner", status="PENDING"),
            "geo_fence": WorkflowStep(session_id="session-1", step_id="geo_fence", status="PENDING"),
            "meta": WorkflowStep(session_id="session-1", step_id="meta", status="PENDING"),
        },
    )

    mapped = map_for_media_planner(session)

    assert mapped["brand_intelligence"]["brand_name"] == "Citimedia"
    assert mapped["brand_intelligence"]["summary"] == "Brand summary"
    assert mapped["audit_findings"]["summary"] == "Audit summary"
    assert mapped["audit_findings"]["technical_issues"] == ["Slow pages"]
    assert "raw" not in mapped["brand_intelligence"]
    assert "raw" not in mapped["audit_findings"]


def test_media_planner_mapping_prunes_empty_fields_from_atlas():
    session = WorkflowSession(
        session_id="session-1",
        url="https://example.com",
        user_id="user_123",
        workflow_status=WorkflowStatus.RUNNING,
        steps={
            "atlas": WorkflowStep(
                session_id="session-1",
                step_id="atlas",
                status="APPROVED",
                approved_output={
                    "client": {"name_extracted": "Citimedia"},
                    "executive_summary": {"snapshot": "Brand summary"},
                },
            ),
            "audit": WorkflowStep(
                session_id="session-1",
                step_id="audit",
                status="APPROVED",
                approved_output={"summary": "Audit summary"},
            ),
            "media_planner": WorkflowStep(session_id="session-1", step_id="media_planner", status="PENDING"),
            "geo_fence": WorkflowStep(session_id="session-1", step_id="geo_fence", status="PENDING"),
            "meta": WorkflowStep(session_id="session-1", step_id="meta", status="PENDING"),
        },
    )

    mapped = map_for_media_planner(session)

    assert mapped["brand_intelligence"] == {
        "brand_name": "Citimedia",
        "summary": "Brand summary",
    }


def test_geo_receives_compact_fields_only():
    session = build_session_with_media_plan_output(
        {
            "content": {
                "brand": {"name": "Citimedia", "goal": "Lead Generation", "primary_audience": "B2B"},
                "geofence_zones": [{"zone_name": "Ameerpet", "city": "Hyderabad", "radius": "3km"}],
                "target_locations": ["Hyderabad"],
                "channel_split": {"Google Search": {"pct": 30}},
                "phases": [{"weeks": "1-4"}],
                "total_monthly_budget": 150000,
                "raw": {"logs": ["secret"]},
                "usage": {"tokens": 123},
                "exec_id": 99,
            }
        }
    )

    mapped = map_for_geo_fence(session)

    assert mapped["brand_name"] == "Citimedia"
    assert mapped["primary_location"] == "Hyderabad"
    assert mapped["target_locations"] == ["Hyderabad"]
    assert mapped["geofence_zones"][0]["zone_name"] == "Ameerpet"
    assert mapped["geofence_zones"][0]["city"] == "Hyderabad"
    assert mapped["campaign_objective"] == "Lead Generation"
    assert mapped["budget"] == 150000
    assert mapped["duration"] == "1-4"
    assert mapped["audience_segments"] == ["B2B"]
    assert mapped["recommended_channels"] == ["Google Search"]
    assert "raw" not in mapped
    assert "usage" not in mapped
    assert "exec_id" not in mapped


def test_meta_receives_compact_fields_only_and_special_categories():
    session = build_session_with_media_plan_output(
        {
            "content": {
                "brand": {"name": "Citimedia", "goal": "Lead Generation", "primary_audience": "B2B"},
                "locations": ["Hyderabad"],
                "channel_split": {"Meta": {"pct": 20}, "Google Search": {"pct": 30}},
                "phases": [{"weeks": "1-4", "name": "Build", "channel_tactics": {"Meta": "Cold ads"}}],
                "total_monthly_budget": 150000,
                "ad_creatives": ["Static image"],
                "raw": {"logs": ["secret"]},
                "usage": {"tokens": 123},
            }
        }
    )

    mapped = map_for_meta(session)

    assert mapped["brand_name"] == "Citimedia"
    assert mapped["campaign_objective"] == "Lead Generation"
    assert mapped["target_audience"] == "B2B"
    assert mapped["locations"] == ["Hyderabad"]
    assert mapped["budget"] == 150000
    assert mapped["daily_budget"] == 5000
    assert mapped["duration"] == "1-4"
    assert mapped["campaign_name"] == "Citimedia - Lead Generation"
    assert mapped["ad_sets"][0]["name"] == "Build"
    assert mapped["ad_creatives"] == [{"format": "Static image", "brief": "Static image"}]
    assert mapped["placements"] == ["Facebook Feed", "Instagram Feed", "Instagram Stories"]
    assert mapped["special_ad_categories"] == []
    assert "raw" not in mapped
    assert "usage" not in mapped


def test_meta_defaults_fill_missing_structured_fields():
    session = build_session_with_media_plan_output(
        {
            "content": {
                "brand": {"name": "Citimedia", "goal": "Awareness", "primary_audience": "Parents"},
                "target_locations": ["Hyderabad"],
                "total_monthly_budget": "90000",
                "phases": [{"weeks": "1-2", "phase": "Launch"}],
            }
        }
    )

    mapped = map_for_meta(session)

    assert mapped["locations"] == ["Hyderabad"]
    assert mapped["daily_budget"] == 3000
    assert mapped["ad_sets"][0]["name"] == "Launch"
    assert mapped["ad_creatives"][0]["format"] == "Static Image"
    assert mapped["special_ad_categories"] == []
