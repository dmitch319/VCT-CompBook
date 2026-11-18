"""Synthetic vlrggapi responses, shaped like the real ones at the vlrggapi commit pinned in
docker-compose.yml. Swap in real saved responses (tests/data/*.json) once the self-hosted
vlrggapi is running; these keep the parser honest about the shape until then.

The story: event 2001 (tier 1) has a completed match 1001, Team Alpha (ALP) beats
Team Bravo (BRV) 2-0 on Ascent 13-9 and Lotus 13-11; Sunset was the unplayed decider.
"""

from typing import Any

EVENT_ID = 2001
MATCH_ID = 1001
ALPHA, BRAVO = 11, 22


def envelope(segments: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "success", "data": {"status": 200, "segments": segments}}


def events_body() -> dict[str, Any]:
    return envelope(
        [
            {
                "title": "Champions Tour 2026: Masters Santiago",
                "event_id": str(EVENT_ID),
                "status": "ongoing",
                "prize": "$1,000,000",
                "dates": "Mar 1—16",
                "region": "cl",
                "thumb": "",
                "url_path": f"https://www.vlr.gg/event/{EVENT_ID}/x",
            },
            {
                "title": "Challengers 2026: North America Stage 1",
                "event_id": "2002",
                "status": "ongoing",
                "prize": "",
                "dates": "Feb 1—Mar 30",
                "region": "us",
                "thumb": "",
                "url_path": "https://www.vlr.gg/event/2002/y",
            },
        ]
    )


def event_matches_body() -> dict[str, Any]:
    def listed(match_id: int, status: str, ts: str) -> dict[str, Any]:
        return {
            "match_id": str(match_id),
            "url": f"https://www.vlr.gg/{match_id}/x",
            "date": "Tue, March 10, 2026",
            "time": "6:00 PM",
            "unix_timestamp": ts,
            "status": status,
            "note": "",
            "stage": "Playoffs",
            "event_series": "Upper Final",
            "team1": {"name": "Team Alpha", "score": "2", "is_winner": True},
            "team2": {"name": "Team Bravo", "score": "0", "is_winner": False},
        }

    return envelope(
        [
            listed(MATCH_ID, "Completed", "2026-03-10 18:00:00"),
            listed(1002, "Upcoming", ""),
        ]
    )


def _player(name: str, agent: str, rating: str = "1.10") -> dict[str, str]:
    return {
        "name": name,
        "agent": agent,
        "rating": rating,
        "acs": "230",
        "kills": "18",
        "deaths": "15",
        "assists": "6",
        "kd_diff": "+3",
        "kast": "74%",
        "adr": "151.3",
        "hs_pct": "27%",
        "fk": "3",
        "fd": "2",
        "fk_diff": "+1",
    }


def _rounds(team1_wins: int, team2_wins: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rounds where team1 starts on defense (ct) and wins its rounds first; returns the
    round list and the score_ct / score_t splits that go with it."""
    rounds = []
    split = {"score_ct": {"team1": 0, "team2": 0}, "score_t": {"team1": 0, "team2": 0}}
    winners = ["team1"] * team1_wins + ["team2"] * team2_wins
    # Interleave so both halves have winners for both teams
    winners = winners[::2] + winners[1::2]
    for i, winner in enumerate(winners, start=1):
        team1_side = "ct" if i <= 12 else "t"
        side = team1_side if winner == "team1" else ("t" if team1_side == "ct" else "ct")
        key = "score_ct" if side == "ct" else "score_t"
        split[key][winner] += 1
        rounds.append({"round_num": i, "winner": winner, "side": side, "win_type": "elim"})
    as_text = {k: {t: str(v) for t, v in d.items()} for k, d in split.items()}
    return rounds, as_text


def _map(
    name: str, picked_by: str, s1: int, s2: int, alpha: list[str], bravo: list[str]
) -> dict[str, Any]:
    rounds, split = _rounds(s1, s2)
    return {
        "map_name": name,
        "picked_by": picked_by,
        "duration": "41:07",
        "score": {"team1": s1, "team2": s2},
        "score_ct": split["score_ct"],
        "score_t": split["score_t"],
        "score_ot": {"team1": "", "team2": ""},
        "players": {
            "team1": [_player(f"alp_{i}", a) for i, a in enumerate(alpha, start=1)],
            "team2": [_player(f"brv_{i}", a) for i, a in enumerate(bravo, start=1)],
        },
        "rounds": rounds,
    }


ALPHA_COMP = ["Jett", "Sova", "Omen", "Killjoy", "Fade"]  # 1D-2I-1C-1S
BRAVO_COMP_1 = ["Raze", "Breach", "Viper", "Omen", "Cypher"]  # 1D-1I-2C-1S
BRAVO_COMP_2 = ["Neon", "KAY/O", "Harbor", "Viper", "Chamber"]  # 1D-1I-2C-1S


def match_detail_body() -> dict[str, Any]:
    unplayed = {
        "map_name": "Sunset",
        "picked_by": "",
        "duration": "",
        "score": {"team1": "", "team2": ""},
        "score_ct": {"team1": "", "team2": ""},
        "score_t": {"team1": "", "team2": ""},
        "score_ot": {"team1": "", "team2": ""},
        "players": {"team1": [], "team2": []},
        "rounds": [],
    }
    return envelope(
        [
            {
                "match_id": str(MATCH_ID),
                "event": {
                    "name": "Champions Tour 2026: Masters Santiago",
                    "series": "Playoffs: Upper Final",
                    "logo": "",
                },
                "date": "Tuesday, March 10th 6:00 PM PDT Patch 12.04",
                "map_vetos": (
                    "ALP ban Bind; BRV ban Haven; ALP pick Ascent; BRV pick Lotus; "
                    "ALP ban Split; BRV ban Icebox; Sunset remains"
                ),
                "status": "final",
                "teams": [
                    {
                        "id": str(ALPHA),
                        "name": "Team Alpha",
                        "tag": "ALP",
                        "logo": "",
                        "score": "2",
                        "is_winner": True,
                    },
                    {
                        "id": str(BRAVO),
                        "name": "Team Bravo",
                        "tag": "BRV",
                        "logo": "",
                        "score": "0",
                        "is_winner": False,
                    },
                ],
                "streams": [],
                "vods": [],
                "maps": [
                    _map("Ascent", "Team Alpha", 13, 9, ALPHA_COMP, BRAVO_COMP_1),
                    _map("Lotus", "Team Bravo", 13, 11, ALPHA_COMP, BRAVO_COMP_2),
                    unplayed,
                ],
                "head_to_head": [],
                "performance": {
                    "kill_matrix": [],
                    "advanced_stats": [],
                    "by_map": [
                        {"game_id": "5001", "kill_matrix": [], "advanced_stats": []},
                        {"game_id": "5002", "kill_matrix": [], "advanced_stats": []},
                    ],
                },
                "economy": [],
                "economy_by_map": [],
            }
        ]
    )
