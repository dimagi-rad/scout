"""Agent tool: define a cross-opp canonical measure on demand (the chat-driven auto-model)."""

from __future__ import annotations

from langchain_core.tools import tool


def create_crossopp_measure_tools(workspace, user, conversation_id=None) -> list:
    @tool
    async def define_crossopp_measure(name: str, description: str, kind: str = "numeric") -> dict:
        """Define a NEW cross-opp canonical measure so it can be compared across every
        opportunity. Use when a cross-opp question needs a measure that semantic_catalog
        does not list. `name` is a snake_case slug; `description` is one plain-language
        line of what it means; `kind` is 'numeric' (a value to average) or 'rate' (a
        boolean event averaged into a 0..1 rate). If the resolver is unsure for any opp,
        this returns needs_approval and the user is asked to confirm before it commits."""
        from asgiref.sync import sync_to_async

        from apps.transformations.models import CrossOppMeasure, CrossOppMeasureDraft
        from apps.transformations.services import crossopp_measure_service as svc
        from apps.transformations.services.measure_resolver import CanonicalMeasureSpec

        spec = CanonicalMeasureSpec(name=name.strip(), description=description.strip(), kind=kind)

        if await CrossOppMeasure.objects.filter(workspace=workspace, name=spec.name).aexists():
            return {
                "status": "exists",
                "measure": spec.name,
                "message": f"'{spec.name}' is already defined.",
            }

        opps, cands = await sync_to_async(svc.workspace_opps)(workspace)
        resolutions = await svc.resolve_across_opps_from_candidates(spec, cands)
        has_doubt, flagged = svc.classify_doubt(resolutions)

        if not has_doubt:
            lineage = await svc.aadd_measure(workspace, spec, resolutions, opps)
            await svc.ensure_measure_queryable_meta(workspace, spec.name)
            return {
                "status": "committed",
                "measure": spec.name,
                "lineage": lineage,
                "message": f"Defined '{spec.name}' across {len(opps)} opportunities.",
            }

        draft = await CrossOppMeasureDraft.objects.acreate(
            workspace=workspace,
            name=spec.name,
            description=spec.description,
            kind=spec.kind,
            thread_id=conversation_id or "",
            created_by=user,
            status="pending",
            resolutions={o: svc.serialize_resolution(r) for o, r in resolutions.items()},
            flagged=flagged,
            shortlists={o: svc.shortlist_for_opp(cands.get(o, [])) for o in flagged},
        )
        return {
            "status": "needs_approval",
            "draft_id": str(draft.id),
            "measure": spec.name,
            "flagged": [
                {
                    "opp_id": o,
                    "guess": resolutions[o].column,
                    "confidence": resolutions[o].confidence,
                    "shortlist": draft.shortlists[o],
                }
                for o in flagged
            ],
            "resolved": [
                {"opp_id": o, "column": r.column, "confidence": r.confidence}
                for o, r in resolutions.items()
                if o not in flagged
            ],
            "message": (
                f"Resolved '{spec.name}' but {len(flagged)} opp(s) need your confirmation."
            ),
        }

    define_crossopp_measure.name = "define_crossopp_measure"

    @tool
    async def propose_crossopp_measures(limit: int = 8) -> dict:
        """Propose the measures most worth comparing across these opportunities (reads the
        apps). Commits the confident ones and returns any that need your approval."""
        from asgiref.sync import sync_to_async

        from apps.transformations.models import CrossOppMeasure, CrossOppMeasureDraft
        from apps.transformations.services import crossopp_measure_proposer as prop
        from apps.transformations.services import crossopp_measure_service as svc

        opps, cands = await sync_to_async(svc.workspace_opps)(workspace)
        specs = await prop.propose_measures(cands, limit=limit)
        committed, pending = [], []
        for spec in specs:
            if await CrossOppMeasure.objects.filter(workspace=workspace, name=spec.name).aexists():
                continue
            resolutions = await svc.resolve_across_opps_from_candidates(spec, cands)
            has_doubt, flagged = svc.classify_doubt(resolutions)
            if not has_doubt:
                await svc.aadd_measure(workspace, spec, resolutions, opps)
                committed.append(spec.name)
            else:
                d = await CrossOppMeasureDraft.objects.acreate(
                    workspace=workspace,
                    name=spec.name,
                    description=spec.description,
                    kind=spec.kind,
                    thread_id=conversation_id or "",
                    created_by=user,
                    status="pending",
                    resolutions={o: svc.serialize_resolution(r) for o, r in resolutions.items()},
                    flagged=flagged,
                    shortlists={
                        o: svc.shortlist_for_opp(cands.get(o, [])) for o in flagged
                    },
                )
                pending.append({"measure": spec.name, "draft_id": str(d.id), "flagged": flagged})
        if committed:
            await svc.ensure_measure_queryable_meta(workspace, committed[-1])
        return {
            "status": "proposed",
            "committed": committed,
            "needs_approval": pending,
            "message": f"Committed {len(committed)}; {len(pending)} need approval.",
        }

    propose_crossopp_measures.name = "propose_crossopp_measures"

    @tool
    async def define_crossopp_visit_field(name: str, description: str) -> dict:
        """Define a NEW cross-opp canonical PER-VISIT field — a value recorded at each
        visit that you want to GROUP BY or trend over time, not just average once.

        Use this for longitudinal / over-time / growth-curve questions. Unlike
        define_crossopp_measure (one averaged number per opp), a visit field becomes a
        per-visit dimension/measure in the semantic layer. To assemble an infant
        growth curve, define the fields named exactly 'visit_weight' (the infant's weight
        recorded at the visit) and 'age_days' (the infant's age in days at the visit), and
        make sure a 'birth_weight' measure is also defined (define_crossopp_measure) — the
        cube then exposes age_days + birthweight_band dimensions and avg_visit_weight (with
        a 95% CI) so you can plot weight-by-age per birth-weight band across every opp.

        `name` is a snake_case slug; `description` is one plain-language line. The resolver
        aligns each opp's real column (apps differ), using sample values to avoid
        placeholder/garbage columns. Low-confidence opps are committed but flagged in the
        returned lineage so you can show the user exactly how each opp was mapped."""
        from asgiref.sync import sync_to_async

        from apps.transformations.models import CrossOppMeasureLineage
        from apps.transformations.services import crossopp_measure_service as svc
        from apps.transformations.services.measure_resolver import CanonicalMeasureSpec

        fname = name.strip()
        spec = CanonicalMeasureSpec(name=fname, description=description.strip(), kind="numeric")

        if await CrossOppMeasureLineage.objects.filter(
            workspace=workspace, measure=fname
        ).aexists():
            return {
                "status": "exists",
                "field": fname,
                "message": f"Per-visit field '{fname}' is already defined.",
            }

        opps, cands = await sync_to_async(svc.workspace_opps)(workspace)
        resolutions = await svc.resolve_across_opps_from_candidates(spec, cands)
        _has_doubt, flagged = svc.classify_doubt(resolutions)

        await sync_to_async(svc.add_visit_field)(workspace, fname, resolutions, opps)
        # visit_weight is what produces the avg_visit_weight measure in the cube; poll it so
        # the surface is queryable before the agent charts it. age_days is a dimension only.
        if fname == "visit_weight":
            await svc.ensure_measure_queryable_meta(workspace, "avg_visit_weight")

        lineage = [
            {
                "opportunity_id": o,
                "column": r.column,
                "confidence": r.confidence,
                "status": r.status,
                "sql_expression": r.sql_expression,
            }
            for o, r in resolutions.items()
        ]
        msg = f"Defined per-visit field '{fname}' across {len(opps)} opportunities."
        if flagged:
            msg += f" {len(flagged)} opp(s) were low-confidence — show the lineage so the user can verify the mapping."
        return {"status": "committed", "field": fname, "lineage": lineage, "flagged": flagged, "message": msg}

    define_crossopp_visit_field.name = "define_crossopp_visit_field"

    return [define_crossopp_measure, propose_crossopp_measures, define_crossopp_visit_field]
