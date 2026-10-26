"""Evidence-backed identity/state reconciliation; representation never assigns ownership.

DuckDB performs the corpus-sized comparisons under its configured memory limit.
Only the small actor-parent graph is materialized in Python. Source observations
remain available in raw_*; derived rows retain immutable evidence references.
"""

from __future__ import annotations

import json

import pyarrow as pa

RECONCILIATION = r"""
CREATE TEMP TABLE observations AS
SELECT *, CASE WHEN uuid IS NOT NULL THEN 'uuid:' || uuid
 ELSE 'local:' || source_file || ':' || line_no END AS event_key,
 CASE WHEN message_id IS NOT NULL AND uuid IS NOT NULL THEN 'provider:' || message_id
 WHEN uuid IS NOT NULL THEN 'uuid:' || uuid
 ELSE 'local:' || source_file || ':' || line_no END AS turn_key
FROM raw_fragments;
-- Unknown copies join the one observed provider candidate; contradictory
-- present IDs retain their memberships rather than evicting the shared event.
CREATE TEMP TABLE provider_candidates AS
SELECT event_key,count(DISTINCT message_id) AS provider_count,
 any_value(message_id ORDER BY message_id) AS provider_id
FROM observations GROUP BY event_key;
UPDATE observations SET turn_key='provider:' || p.provider_id
FROM provider_candidates p WHERE observations.event_key=p.event_key
 AND p.provider_count=1 AND observations.message_id IS NULL;
-- An unknown copy is evidence for the known candidates, not another message.
UPDATE observations SET turn_key=NULL FROM provider_candidates p
WHERE observations.event_key=p.event_key AND p.provider_count>1
 AND observations.message_id IS NULL;
CREATE TEMP TABLE turn_events AS
SELECT DISTINCT turn_key,event_key FROM observations WHERE turn_key IS NOT NULL;

CREATE TEMP TABLE event_states AS
SELECT event_key, count(DISTINCT message_id)>1 AS identity_conflict,
 count(DISTINCT content_hash) > 1 AS content_conflict,
 count(DISTINCT immutable_hash) > 1 OR count(DISTINCT request_id_hash) > 1 AS immutable_conflict,
 count(DISTINCT payload_hash) FILTER(WHERE stop_reason IS NOT NULL) AS terminal_variants,
 count(DISTINCT payload_hash) AS payload_variants,
 list(DISTINCT struct_pack(source_file := source_file, line_no := line_no)
      ORDER BY struct_pack(source_file := source_file, line_no := line_no)) AS source_references
FROM observations GROUP BY event_key;

CREATE TEMP TABLE canonical_fragments AS
SELECT o.*, s.content_conflict,s.identity_conflict,
 (s.immutable_conflict OR s.terminal_variants > 1 OR
  (s.terminal_variants = 0 AND s.payload_variants > 1)) AS state_conflict,
 s.source_references
FROM observations o JOIN event_states s USING(event_key)
QUALIFY row_number() OVER(PARTITION BY event_key ORDER BY
 (stop_reason IS NOT NULL) DESC, source_file, line_no, payload_hash) = 1;

CREATE VIEW fragments AS SELECT * FROM canonical_fragments;

-- Every physical source contributes its distinct ordered identity sequence.
CREATE TEMP TABLE message_sequences AS
SELECT turn_key, source_file, list(event_key ORDER BY line_no) AS sequence
FROM (SELECT * FROM observations WHERE turn_key IS NOT NULL
 QUALIFY row_number() OVER(PARTITION BY source_file,event_key,turn_key ORDER BY line_no)=1)
GROUP BY turn_key, source_file;

CREATE TEMP TABLE representation AS
SELECT c.*, NOT EXISTS (
 SELECT 1 FROM message_sequences other WHERE other.turn_key=c.turn_key
 AND NOT _sequence_contains(c.sequence,other.sequence)
) AS assembly_complete FROM message_sequences c;

CREATE TEMP TABLE message_copy AS
SELECT * FROM representation
QUALIFY row_number() OVER(PARTITION BY turn_key ORDER BY assembly_complete DESC,
 len(sequence) DESC, source_file)=1;

-- Main-prefix membership is structural inference, not producer provenance.
CREATE TEMP TABLE inherited AS
WITH shared AS (
 SELECT c.source_file, c.line_no, c.event_key, bool_or(m.event_key IS NOT NULL) AS shared
 FROM observations c LEFT JOIN observations m ON c.event_key=m.event_key
 AND c.context_session_id=m.context_session_id AND m.source_role='main'
 WHERE c.source_role='child' GROUP BY c.source_file,c.line_no,c.event_key
), boundary AS (
 SELECT source_file,min(line_no) FILTER(WHERE NOT shared) AS first_unique FROM shared GROUP BY source_file
)
SELECT s.source_file,s.event_key FROM shared s JOIN boundary b USING(source_file)
WHERE s.shared AND (b.first_unique IS NULL OR s.line_no < b.first_unique);

CREATE TEMP TABLE event_ownership AS
WITH candidates AS (
 SELECT o.*, i.event_key IS NOT NULL AS inherited FROM observations o
 LEFT JOIN inherited i USING(source_file,event_key)
), g AS (
 SELECT event_key,count(DISTINCT actor_id) AS all_actors,
 count(DISTINCT actor_id) FILTER(WHERE NOT inherited) AS remaining,
 any_value(actor_id ORDER BY actor_id) FILTER(WHERE NOT inherited) AS executor,
 bool_or(inherited) AS has_inference,bool_or(context_conflict) AS context_conflict,
 list(DISTINCT context_session_id ORDER BY context_session_id) AS context_sessions
 FROM candidates GROUP BY event_key
)
SELECT g.*, CASE WHEN s.content_conflict OR g.context_conflict THEN 'conflicting'
 WHEN remaining=1 THEN CASE WHEN has_inference THEN 'inferred' ELSE 'confirmed' END
 ELSE 'unresolved' END AS attribution_status,
 CASE WHEN remaining=1 AND NOT s.content_conflict AND NOT g.context_conflict THEN executor END AS executor_id,
 CASE WHEN g.context_conflict THEN 'source path contradicts record context/actor'
 WHEN s.content_conflict THEN 'conflicting identity content'
 WHEN remaining=1 AND has_inference THEN 'shared containing-main prefix'
 WHEN remaining=1 THEN 'unique observed actor'
 ELSE 'multiple or missing observed actors' END AS attribution_reason
FROM g JOIN event_states s USING(event_key);

-- Evidence/availability precede presentation selection. Every distinct event
-- and physical observation remains in the envelope even on divergent copies.
CREATE TEMP TABLE turn_evidence AS
WITH events AS (
 SELECT c.* EXCLUDE(turn_key),membership.turn_key,
 o.attribution_status,o.attribution_reason,o.executor_id,o.context_sessions
 FROM turn_events membership JOIN canonical_fragments c USING(event_key)
 JOIN event_ownership o USING(event_key)
), grouped AS (
 SELECT turn_key,count(*) AS distinct_event_count,
 bool_or(content_conflict) AS content_conflict,bool_or(state_conflict) AS state_conflict,
 bool_or(identity_conflict) AS identity_conflict,bool_or(identity_conflict) AS count_uncertain,
 CASE WHEN NOT bool_or(identity_conflict) AND count(DISTINCT executor_id)=1 AND count(*) FILTER(WHERE executor_id IS NULL)=0
 THEN any_value(executor_id) END AS executor_id,
 CASE WHEN bool_or(identity_conflict) OR bool_or(attribution_status='conflicting') THEN 'conflicting'
 WHEN count(DISTINCT executor_id)!=1 OR count(*) FILTER(WHERE executor_id IS NULL)>0 THEN 'unresolved'
 WHEN bool_or(attribution_status='inferred') THEN 'inferred' ELSE 'confirmed' END AS attribution_status,
 list_sort(list_distinct(list(attribution_reason) ||
 CASE WHEN bool_or(identity_conflict) THEN ['event links multiple provider candidates'] ELSE [] END)) AS attribution_reasons,
 list_sort(list_distinct(flatten(list(context_sessions ORDER BY event_key)))) AS context_sessions
 FROM events GROUP BY turn_key
), physical AS (
 SELECT membership.turn_key,count(*) AS physical_observation_count,
 bool_or(is_api_error) AS is_api_error,bool_or(scrub_failed) AS scrub_failed,
 count(DISTINCT terminal_hash) FILTER(WHERE stop_reason IS NOT NULL) AS terminal_variants,
 list(DISTINCT struct_pack(source_file:=source_file,line_no:=line_no)
      ORDER BY struct_pack(source_file:=source_file,line_no:=line_no)) AS source_references
 FROM turn_events membership JOIN observations USING(event_key) GROUP BY membership.turn_key
)
SELECT * FROM grouped JOIN physical USING(turn_key);

CREATE TEMP TABLE assembled_fragments AS
SELECT c.* EXCLUDE(turn_key,message_id),m.turn_key,
 CASE WHEN starts_with(m.turn_key,'provider:') THEN substring(m.turn_key,10)
 ELSE physical.message_id END AS message_id,
 m.assembly_complete, o.attribution_status,o.attribution_reason,o.executor_id,o.context_sessions,
 physical.line_no AS sequence_line, physical.source_file AS representation_source
FROM message_copy m JOIN observations physical ON physical.turn_key=m.turn_key AND physical.source_file=m.source_file
JOIN canonical_fragments c ON c.event_key=physical.event_key
JOIN event_ownership o ON o.event_key=c.event_key
QUALIFY row_number() OVER(PARTITION BY m.turn_key,c.event_key ORDER BY physical.line_no)=1;
"""


def _parent_graph(rows):
    """Resolve distinct immediate-parent assertions, rejecting contradictions/cycles."""
    evidence = {}
    explicit = {}
    for actor, parent, status, kind, ref in rows:
        evidence.setdefault(actor, []).append((parent, status, kind, ref))
        if kind == "parentAgentId":
            explicit.setdefault(actor, set()).add(parent)
    resolved = {}
    for actor, assertions in sorted(evidence.items()):
        assertions = sorted(assertions, key=lambda value: json.dumps(value, ensure_ascii=True))
        parents = {a[0] for a in assertions if a[0] is not None}
        direct = explicit.get(actor, set())
        association_conflict = any(a[1] == "conflicting" and a[2] == "spawn association" for a in assertions)
        if association_conflict:
            status, parent, reason = "conflicting", None, "contradictory child association"
        elif len(parents) > 1 or len(direct) > 1 or actor in parents:
            status, parent, reason = "conflicting", None, "contradictory parent assertions"
        elif len(parents) == 1:
            parent = next(iter(parents))
            proof = [a for a in assertions if a[0] == parent and a[2] != "parentAgentId"]
            status = (
                "confirmed"
                if proof and any(a[1] == "confirmed" for a in proof)
                else "inferred"
                if proof
                else "unresolved"
            )
            reason = (
                "resolved spawning-call producer"
                if proof
                else "parent asserted without spawning-call producer"
            )
            if not proof:
                parent = None
        else:
            status, parent, reason = "unresolved", None, "no resolved spawning-call producer"
        resolved[actor] = dict(
            actor_id=actor,
            parent_actor_id=parent,
            lineage_status=status,
            lineage_reason=reason,
            lineage_evidence=json.dumps(assertions, ensure_ascii=True),
        )
    visited = set()
    for actor in resolved:
        path, positions, current = [], {}, actor
        while current in resolved and current not in visited and current not in positions:
            positions[current] = len(path)
            path.append(current)
            current = resolved[current]["parent_actor_id"]
        if current in positions:
            for node in path[positions[current] :]:
                resolved[node].update(
                    parent_actor_id=None,
                    lineage_status="conflicting",
                    lineage_reason="parent cycle",
                )
        visited.update(path)
    return list(resolved.values())


def _sequence_contains(sequence, candidate):
    remaining = iter(sequence)
    return all(any(value == observed for observed in remaining) for value in candidate)


def _spawn_associations(con):
    """Reconcile child identity before an assertion can enter the parent graph."""
    con.execute("""
      CREATE TEMP TABLE result_candidates AS
      SELECT r.* EXCLUDE(result_agent_id,result_agent_ids),candidate AS result_agent_id
      FROM raw_tool_calls r,unnest(r.result_agent_ids) identities(candidate);

      CREATE TEMP TABLE producers AS
      SELECT c.*,o.executor_id,o.attribution_status FROM raw_tool_calls c
      JOIN tool_ownership o USING(tool_use_id)
      WHERE c.line_no_call IS NOT NULL AND c.tool IN ('Agent','Task');

      CREATE TEMP TABLE companion_assertions AS
      SELECT a.tool_use_id,a.actor_id,
        coalesce(p.executor_id,CASE WHEN a.parent_agent_id IS NOT NULL
          AND p.attribution_status!='conflicting' AND NOT p.context_conflict
          AND p.actor_id=_actor_identity(a.context_session_id,a.parent_agent_id)
          THEN p.actor_id END) AS parent,
        CASE WHEN p.executor_id IS NULL AND a.parent_agent_id IS NOT NULL
          AND p.attribution_status!='conflicting' AND NOT p.context_conflict
          AND p.actor_id=_actor_identity(a.context_session_id,a.parent_agent_id)
          THEN 'confirmed' ELSE p.attribution_status END AS status,
        to_json(struct_pack(child_source:=a.source_file,call_source:=p.source_file,
          call_line:=p.line_no_call,tool_use_id:=a.tool_use_id)) AS ref
      FROM raw_agents a LEFT JOIN producers p USING(tool_use_id)
      WHERE a.tool_use_id IS NOT NULL;

      CREATE TEMP TABLE spawn_associations AS
      WITH calls AS (
        SELECT tool_use_id,any_value(executor_id) AS executor_id,
          any_value(attribution_status) AS producer_status FROM producers GROUP BY tool_use_id
      ), companions AS (
        SELECT tool_use_id,count(DISTINCT actor_id) FILTER(
          WHERE parent IS NOT NULL AND status IN ('confirmed','inferred')) AS companion_actors,
          any_value(actor_id ORDER BY actor_id) FILTER(
          WHERE parent IS NOT NULL AND status IN ('confirmed','inferred')) AS companion_actor,
          CASE WHEN bool_or(status='inferred') FILTER(WHERE parent IS NOT NULL)
          THEN 'inferred' ELSE 'confirmed' END AS companion_status
        FROM companion_assertions GROUP BY tool_use_id
      ), results AS (
        SELECT tool_use_id,count(DISTINCT result_agent_id) AS result_ids,
          count(DISTINCT _actor_identity(context_session_id,result_agent_id)) AS result_actors,
          any_value(result_agent_id ORDER BY result_agent_id) AS result_id
        FROM result_candidates GROUP BY tool_use_id
      ), evidence AS (
        SELECT calls.*,coalesce(companion_actors,0) AS companion_actors,companion_actor,companion_status,
          coalesce(result_ids,0) AS result_ids,coalesce(result_actors,0) AS result_actors,result_id
        FROM calls LEFT JOIN companions USING(tool_use_id) LEFT JOIN results USING(tool_use_id)
      ) SELECT *,CASE
        WHEN companion_actors>1 OR result_ids>1 OR
          (companion_actors=1 AND result_ids=1 AND result_id!=json_extract_string(companion_actor,'$[1]'))
          THEN 'conflicting'
        WHEN companion_actors=1 THEN companion_status
        WHEN result_ids=1 AND result_actors=1 AND executor_id IS NOT NULL
          AND producer_status IN ('confirmed','inferred') THEN producer_status
        ELSE 'unresolved' END AS association_status
      FROM evidence;
    """)


def install(con):
    con.create_function(
        "_actor_identity",
        lambda context, agent: json.dumps(
            [context, agent], ensure_ascii=True, separators=(",", ":")
        ),
        ["VARCHAR", "VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    con.create_function(
        "_sequence_contains", _sequence_contains, ["VARCHAR[]", "VARCHAR[]"], "BOOLEAN"
    )
    con.execute(RECONCILIATION)
    # Tool/result assertions are interpreted only for an observed Agent/Task call.
    con.execute("""
      CREATE TEMP TABLE tool_ownership AS
      SELECT c.tool_use_id,
       CASE WHEN count(DISTINCT c.call_hash)=1 AND count(DISTINCT o.executor_id)=1
       AND count(*) FILTER(WHERE o.executor_id IS NULL)=0 THEN any_value(o.executor_id) END AS executor_id,
       CASE WHEN count(DISTINCT c.call_hash)>1 OR bool_or(o.attribution_status='conflicting') THEN 'conflicting'
       WHEN count(DISTINCT o.executor_id)!=1 OR count(*) FILTER(WHERE o.executor_id IS NULL)>0 THEN 'unresolved'
       WHEN bool_or(o.attribution_status='inferred') THEN 'inferred' ELSE 'confirmed' END AS attribution_status,
       'reconciled call observations' AS attribution_reason,
       list_sort(list_distinct(flatten(list(o.context_sessions ORDER BY c.source_file,c.line_no_call)))) AS context_sessions
      FROM raw_tool_calls c LEFT JOIN event_ownership o ON o.event_key=coalesce('uuid:' || c.call_uuid,'local:' || c.source_file || ':' || c.line_no_call)
      WHERE c.line_no_call IS NOT NULL GROUP BY c.tool_use_id
    """)
    _spawn_associations(con)
    rows = con.execute("""
      WITH assertions AS (
       SELECT c.actor_id,c.parent,c.status,'companion toolUseId' AS kind,c.ref
       FROM companion_assertions c LEFT JOIN spawn_associations s USING(tool_use_id)
       WHERE c.parent IS NOT NULL AND c.status IN ('confirmed','inferred') AND s.companion_actors=1
       UNION ALL
       SELECT c.actor_id,NULL,'unresolved','companion toolUseId',c.ref
       FROM companion_assertions c LEFT JOIN spawn_associations s USING(tool_use_id)
       WHERE c.parent IS NULL OR s.tool_use_id IS NULL
       UNION ALL
       SELECT a.actor_id, _actor_identity(a.context_session_id,a.parent_agent_id), 'unresolved',
              'parentAgentId',to_json(struct_pack(source_file:=a.source_file,parent_agent_id:=a.parent_agent_id)) FROM raw_agents a WHERE a.parent_agent_id IS NOT NULL
       UNION ALL
       SELECT CASE WHEN s.companion_actors=1 THEN s.companion_actor
              ELSE _actor_identity(r.context_session_id,r.result_agent_id) END,
              p.executor_id,p.attribution_status,
              'Agent/Task result',to_json(struct_pack(result_source:=r.source_file,
                result_line:=r.line_no_result,call_source:=p.source_file,call_line:=p.line_no_call,
                tool_use_id:=r.tool_use_id)) FROM result_candidates r JOIN producers p USING(tool_use_id)
       JOIN spawn_associations s USING(tool_use_id)
       WHERE r.result_agent_id IS NOT NULL AND s.result_ids=1 AND s.companion_actors<=1
         AND (s.companion_actors=1 OR s.result_actors=1)
         AND s.association_status!='conflicting' AND p.executor_id IS NOT NULL
         AND p.attribution_status IN ('confirmed','inferred')
       UNION ALL
       SELECT _actor_identity(r.context_session_id,r.result_agent_id),NULL,'conflicting',
         'spawn association',to_json(struct_pack(result_source:=r.source_file,
           result_line:=r.line_no_result,tool_use_id:=r.tool_use_id))
       FROM result_candidates r JOIN spawn_associations s USING(tool_use_id)
       WHERE r.result_agent_id IS NOT NULL AND s.association_status='conflicting'
         AND NOT (s.companion_actors=1 AND
           _actor_identity(r.context_session_id,r.result_agent_id)=s.companion_actor)
       UNION ALL
       SELECT c.actor_id,NULL,'conflicting','spawn association',c.ref
       FROM companion_assertions c JOIN spawn_associations s USING(tool_use_id)
       WHERE s.companion_actors>1
       UNION ALL
       SELECT _actor_identity(r.context_session_id,r.result_agent_id),NULL,'unresolved',
         'spawn association',to_json(struct_pack(result_source:=r.source_file,
           result_line:=r.line_no_result,tool_use_id:=r.tool_use_id))
       FROM result_candidates r JOIN spawn_associations s USING(tool_use_id)
       WHERE r.result_agent_id IS NOT NULL AND s.association_status='unresolved'
       UNION ALL
       SELECT actor_id,NULL,NULL,'source',to_json(struct_pack(source_file:=source_file)) FROM observations WHERE source_role='child'
      ) SELECT DISTINCT * FROM assertions
    """).fetchall()
    schema = pa.schema(
        [
            (k, pa.string())
            for k in (
                "actor_id",
                "parent_actor_id",
                "lineage_status",
                "lineage_reason",
                "lineage_evidence",
            )
        ]
    )
    con.register("_actor_lineage", pa.Table.from_pylist(_parent_graph(rows), schema=schema))
    con.execute("CREATE VIEW actor_lineage AS SELECT * FROM _actor_lineage")
