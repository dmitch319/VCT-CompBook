"""Data checks run on every parsed match (design: "Data Integrity").

Each map must have 5 players per team, every player an agent we know the role of, and
as many rounds as the final score adds up to. Problems are reported, not fatal: the
match stays in the database and the issues go into sync_runs.details.
"""

from vct.db import Connection


def validate_match(conn: Connection, match_id: int) -> list[str]:
    issues: list[str] = []

    rows = conn.execute(
        """
        SELECT m.map_order, m.map_name, tm.team_id,
               count(pl.player_id)                                  AS players,
               count(*) FILTER (WHERE pl.agent = '')                AS missing_agents,
               string_agg(DISTINCT pl.agent, ', ')
                   FILTER (WHERE pl.agent <> '' AND a.name IS NULL) AS unknown_agents
        FROM maps m
        JOIN team_maps tm ON tm.map_id = m.id
        LEFT JOIN map_players pl ON pl.map_id = m.id AND pl.team_id = tm.team_id
        LEFT JOIN agents a ON lower(a.name) = lower(pl.agent)
        WHERE m.match_id = %s
        GROUP BY m.map_order, m.map_name, tm.team_id
        ORDER BY m.map_order, tm.team_id
        """,
        (match_id,),
    ).fetchall()
    for r in rows:
        where = f"map {r['map_order']} ({r['map_name']}), team {r['team_id']}"
        if r["players"] != 5:
            issues.append(f"{where}: {r['players']} players, expected 5")
        if r["missing_agents"]:
            issues.append(f"{where}: {r['missing_agents']} player(s) with no agent")
        if r["unknown_agents"]:
            issues.append(f"{where}: agent(s) not in seed/agent_roles.csv: {r['unknown_agents']}")

    rows = conn.execute(
        """
        SELECT m.map_order, m.map_name, m.team1_rounds + m.team2_rounds AS expected,
               count(r.round_number) AS recorded
        FROM maps m
        LEFT JOIN rounds r ON r.map_id = m.id
        WHERE m.match_id = %s
        GROUP BY m.id
        HAVING count(r.round_number) <> m.team1_rounds + m.team2_rounds
        ORDER BY m.map_order
        """,
        (match_id,),
    ).fetchall()
    for r in rows:
        issues.append(
            f"map {r['map_order']} ({r['map_name']}): {r['recorded']} rounds recorded, "
            f"score adds up to {r['expected']}"
        )

    row = conn.execute("SELECT count(*) AS n FROM maps WHERE match_id = %s", (match_id,)).fetchone()
    if row is not None and row["n"] == 0:
        issues.append("no played maps")
    return issues
