"""
services/football_data.py
─────────────────────────
Football data enrichment using football-data.org API (free tier).
Extracts team names from Polymarket questions and fetches:
- Team recent form (last 5 matches)
- Head-to-head history
- Current league standings
- Upcoming match details
"""

import logging
import re
from typing import Optional

import httpx

from config.settings import Settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"

# Map common Polymarket team names to football-data.org team names/IDs
# This is a starter set — extend as needed
TEAM_ALIASES = {
    "man city": "Manchester City FC",
    "manchester city": "Manchester City FC",
    "man utd": "Manchester United FC",
    "manchester united": "Manchester United FC",
    "man united": "Manchester United FC",
    "arsenal": "Arsenal FC",
    "liverpool": "Liverpool FC",
    "chelsea": "Chelsea FC",
    "tottenham": "Tottenham Hotspur FC",
    "spurs": "Tottenham Hotspur FC",
    "newcastle": "Newcastle United FC",
    "aston villa": "Aston Villa FC",
    "west ham": "West Ham United FC",
    "brighton": "Brighton & Hove Albion FC",
    "wolves": "Wolverhampton Wanderers FC",
    "bournemouth": "AFC Bournemouth",
    "fulham": "Fulham FC",
    "crystal palace": "Crystal Palace FC",
    "everton": "Everton FC",
    "nottingham forest": "Nottingham Forest FC",
    "brentford": "Brentford FC",
    "real madrid": "Real Madrid CF",
    "barcelona": "FC Barcelona",
    "barca": "FC Barcelona",
    "atletico madrid": "Club Atlético de Madrid",
    "bayern munich": "FC Bayern München",
    "bayern": "FC Bayern München",
    "dortmund": "Borussia Dortmund",
    "borussia dortmund": "Borussia Dortmund",
    "psg": "Paris Saint-Germain FC",
    "paris saint-germain": "Paris Saint-Germain FC",
    "juventus": "Juventus FC",
    "inter milan": "FC Internazionale Milano",
    "inter": "FC Internazionale Milano",
    "ac milan": "AC Milan",
    "napoli": "SSC Napoli",
}


class FootballDataService:
    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.football_data_api_key
        self._http = httpx.Client(
            timeout=10,
            headers={"X-Auth-Token": self.api_key} if self.api_key else {},
        )
        self._team_cache: dict[str, dict] = {}  # name → {id, competitions, ...}

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def get_football_context(self, question: str) -> Optional[dict]:
        """
        Given a Polymarket market question about football,
        extract team names and return enriched context.
        Returns None if not a football question or API unavailable.
        """
        if not self.is_available:
            return None

        teams = self._extract_teams(question)
        if len(teams) < 2:
            return None

        team_a, team_b = teams[0], teams[1]
        logger.info("Football context for: %s vs %s", team_a, team_b)

        context = {
            "match_type": "football",
            "teams": [team_a, team_b],
        }

        # Get team IDs
        id_a = self._find_team_id(team_a)
        id_b = self._find_team_id(team_b)

        # Fetch form for both teams
        if id_a:
            context["team_a_form"] = self._get_team_form(id_a, team_a)
        if id_b:
            context["team_b_form"] = self._get_team_form(id_b, team_b)

        # Try to find the actual match and get head-to-head
        if id_a and id_b:
            h2h = self._get_head_to_head(id_a, id_b)
            if h2h:
                context["head_to_head"] = h2h

        return context

    def _extract_teams(self, question: str) -> list[str]:
        """Extract team names from a market question like 'Will Man City beat Arsenal?'"""
        q_lower = question.lower()

        found = []
        for alias, full_name in TEAM_ALIASES.items():
            if alias in q_lower and full_name not in found:
                found.append(full_name)

        return found[:2]

    def _find_team_id(self, team_name: str) -> Optional[int]:
        """Find team ID by searching cached competition teams."""
        if team_name in self._team_cache:
            return self._team_cache[team_name].get("id")

        # Search across free tier competitions
        for comp in ["PL", "PD", "BL1", "SA", "FL1", "CL"]:
            try:
                resp = self._http.get(f"{BASE_URL}/competitions/{comp}/teams")
                if resp.status_code != 200:
                    continue
                data = resp.json()
                for team in data.get("teams", []):
                    name = team.get("name", "")
                    self._team_cache[name] = {
                        "id": team.get("id"),
                        "short_name": team.get("shortName", ""),
                        "tla": team.get("tla", ""),
                    }
                    if name == team_name:
                        return team["id"]
            except Exception:
                continue

        logger.debug("Team not found: %s", team_name)
        return None

    def _get_team_form(self, team_id: int, team_name: str) -> dict:
        """Get team's last 5 match results."""
        try:
            resp = self._http.get(
                f"{BASE_URL}/teams/{team_id}/matches",
                params={"status": "FINISHED", "limit": 5},
            )
            if resp.status_code != 200:
                return {"team": team_name, "error": "API error"}

            data = resp.json()
            matches = data.get("matches", [])

            results = []
            wins, draws, losses = 0, 0, 0
            goals_for, goals_against = 0, 0

            for m in matches:
                home = m.get("homeTeam", {})
                away = m.get("awayTeam", {})
                score = m.get("score", {}).get("fullTime", {})
                home_goals = score.get("home", 0) or 0
                away_goals = score.get("away", 0) or 0

                is_home = home.get("id") == team_id
                opponent = away.get("name", "?") if is_home else home.get("name", "?")
                gf = home_goals if is_home else away_goals
                ga = away_goals if is_home else home_goals

                if gf > ga:
                    result = "W"
                    wins += 1
                elif gf == ga:
                    result = "D"
                    draws += 1
                else:
                    result = "L"
                    losses += 1

                goals_for += gf
                goals_against += ga

                venue = "H" if is_home else "A"
                results.append(f"{result} {gf}-{ga} vs {opponent} ({venue})")

            return {
                "team": team_name,
                "last_5": results,
                "record": f"W{wins} D{draws} L{losses}",
                "goals": f"GF {goals_for} GA {goals_against}",
            }
        except Exception as exc:
            logger.debug("Failed to get form for team %s: %s", team_id, exc)
            return {"team": team_name, "error": str(exc)}

    def _get_head_to_head(self, team_a_id: int, team_b_id: int) -> Optional[dict]:
        """Get head-to-head by finding a scheduled match between the two teams."""
        try:
            # Find upcoming match between the two teams
            resp = self._http.get(
                f"{BASE_URL}/teams/{team_a_id}/matches",
                params={"status": "SCHEDULED,TIMED", "limit": 20},
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            match_id = None
            for m in data.get("matches", []):
                home_id = m.get("homeTeam", {}).get("id")
                away_id = m.get("awayTeam", {}).get("id")
                if {home_id, away_id} == {team_a_id, team_b_id}:
                    match_id = m.get("id")
                    break

            if not match_id:
                return None

            # Get head-to-head for this match
            resp = self._http.get(f"{BASE_URL}/matches/{match_id}/head2head")
            if resp.status_code != 200:
                return None

            data = resp.json()
            agg = data.get("aggregates", {})

            return {
                "total_matches": agg.get("numberOfMatches", 0),
                "home_wins": agg.get("homeTeam", {}).get("wins", 0),
                "away_wins": agg.get("awayTeam", {}).get("wins", 0),
                "draws": agg.get("draws", 0),
            }
        except Exception as exc:
            logger.debug("H2H lookup failed: %s", exc)
            return None

    def close(self) -> None:
        self._http.close()
