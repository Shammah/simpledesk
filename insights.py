"""Numbers behind the charts, weekly highlights, streaks, badges and company health."""
from datetime import datetime, timedelta, timezone

import config

DAY = "%Y-%m-%d"


def _now():
    return datetime.now(timezone.utc)


def daily(conn, provider_id, days=30, company_id=None, start=None):
    """Tickets per day: [{'day': '2026-10-01', 'label': '1 Oct', 'total': n, 'ai': n}], oldest first."""
    first = start or (_now() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    where, args = "c.provider_id = ? AND t.created_at >= ? AND t.created_at < ?", [
        provider_id, first.strftime("%Y-%m-%d 00:00:00"), (first + timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")]
    if company_id:
        where += " AND t.company_id = ?"
        args.append(company_id)
    rows = {r["d"]: r for r in conn.execute(
        "SELECT substr(t.created_at, 1, 10) AS d, COUNT(*) AS total, SUM(t.resolved_by = 'ai') AS ai"
        f" FROM tickets t JOIN companies c ON c.id = t.company_id WHERE {where} GROUP BY d", args)}
    out = []
    for i in range(days):
        day = first + timedelta(days=i)
        key = day.strftime(DAY)
        r = rows.get(key)
        out.append(dict(day=key, label=f"{day.day} {day:%b}", total=r["total"] if r else 0, ai=(r["ai"] or 0) if r else 0))
    return out


def weekly_reply_hours(conn, provider_id, weeks=8):
    """Average hours to a technician's first reply, per week: [{'label': '6 Oct', 'hours': 1.5 or None}]."""
    monday = (_now() - timedelta(days=_now().weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(weeks - 1, -1, -1):
        start = monday - timedelta(weeks=i)
        row = conn.execute(
            "SELECT AVG((julianday(t.first_reply_at) - julianday(COALESCE(t.waiting_since, t.created_at))) * 24) AS h"
            " FROM tickets t JOIN companies c ON c.id = t.company_id"
            " WHERE c.provider_id = ? AND t.target_met IS NOT NULL AND t.first_reply_at >= ? AND t.first_reply_at < ?",
            (provider_id, start.strftime("%Y-%m-%d 00:00:00"),
             (start + timedelta(weeks=1)).strftime("%Y-%m-%d 00:00:00"))).fetchone()
        h = row["h"]
        out.append(dict(label=f"{start.day} {start:%b}", hours=round(h, 2) if h is not None else None,
                        text="" if h is None else (f"{max(round(h * 60), 1)}m" if h < 1 else f"{h:.1f}h")))
    return out


def streak(conn, provider_id):
    """Days in a row without a technician reply missing its target (since the last miss, or the first ticket)."""
    late_now = conn.execute(
        "SELECT 1 FROM tickets t JOIN companies c ON c.id = t.company_id WHERE c.provider_id = ?"
        " AND t.status = 'open' AND t.target_met IS NULL AND t.due_at < ? LIMIT 1",
        (provider_id, _now().strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    if late_now:
        return 0          # a reply is overdue right now
    last_miss = conn.execute(
        "SELECT MAX(t.first_reply_at) FROM tickets t JOIN companies c ON c.id = t.company_id"
        " WHERE c.provider_id = ? AND t.target_met = 0", (provider_id,)).fetchone()[0]
    since = last_miss or conn.execute(
        "SELECT MIN(t.created_at) FROM tickets t JOIN companies c ON c.id = t.company_id WHERE c.provider_id = ?",
        (provider_id,)).fetchone()[0]
    if not since:
        return 0
    start = datetime.strptime(since[:10], DAY).date() + (timedelta(days=1) if last_miss else timedelta(0))
    return max((_now().date() - start).days + (0 if last_miss else 1), 0)


def highlights(conn, provider_id):
    """The last 7 days in a few lines."""
    since = (_now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    base = " FROM tickets t JOIN companies c ON c.id = t.company_id WHERE c.provider_id = ? AND t.created_at >= ?"
    row = conn.execute("SELECT COUNT(*) AS total, SUM(t.resolved_by = 'ai') AS ai, SUM(t.status = 'resolved') AS resolved"
                       + base, (provider_id, since)).fetchone()
    best = conn.execute(
        "SELECT c.name, AVG(t.rating) AS avg, COUNT(t.rating) AS n" + base +
        " AND t.rating IS NOT NULL GROUP BY c.id ORDER BY avg DESC, n DESC LIMIT 1", (provider_id, since)).fetchone()
    busiest = conn.execute(
        "SELECT c.name, COUNT(*) AS n" + base + " GROUP BY c.id ORDER BY n DESC LIMIT 1", (provider_id, since)).fetchone()
    minutes = conn.execute(
        "SELECT COALESCE(SUM(e.minutes), 0) FROM time_entries e JOIN tickets t ON t.id = e.ticket_id"
        " JOIN companies c ON c.id = t.company_id WHERE c.provider_id = ? AND e.created_at >= ?",
        (provider_id, since)).fetchone()[0]
    ai = row["ai"] or 0
    return dict(total=row["total"] or 0, ai=ai, resolved=row["resolved"] or 0,
                hours_saved=round(ai * config.MINUTES_SAVED_PER_AI_TICKET / 60, 1), minutes=minutes,
                best=best if best and best["n"] else None, busiest=busiest if busiest and busiest["n"] else None)


BADGES = {
    "fastest": ("⚡", "Fastest reply", "quickest average first reply this month"),
    "closer": ("🏁", "Top closer", "most tickets resolved this month"),
    "stars": ("🌟", "Crowd favourite", "most 😀 ratings this month"),
    "on_time": ("⏱", "Always on time", "every reply on time this month (3 or more)"),
}


def badges(conn, provider_id):
    """{tech_id: [(icon, title, why)]} for this month. Needs at least two technicians to compare."""
    month = _now().strftime("%Y-%m-01 00:00:00")
    techs = [r[0] for r in conn.execute(
        "SELECT id FROM technicians WHERE provider_id = ? AND active = 1 AND password_hash != ''", (provider_id,))]
    won = {t: [] for t in techs}
    if len(techs) < 2:
        return won
    base = (" FROM tickets t JOIN companies c ON c.id = t.company_id"
            " WHERE c.provider_id = ? AND t.assigned_to IS NOT NULL AND {col} >= ? GROUP BY t.assigned_to")

    def leader(select, col, order):
        row = conn.execute(f"SELECT t.assigned_to AS who, {select} AS v" + base.format(col=col) +
                           f" HAVING v IS NOT NULL AND v > 0 ORDER BY v {order} LIMIT 1", (provider_id, month)).fetchone()
        return row["who"] if row else None

    picks = {
        "fastest": leader("AVG(julianday(t.first_reply_at) - julianday(COALESCE(t.waiting_since, t.created_at)))",
                          "t.first_reply_at", "ASC"),
        "closer": leader("SUM(t.status = 'resolved')", "t.resolved_at", "DESC"),
        "stars": leader("SUM(t.rating = 3)", "t.resolved_at", "DESC"),
    }
    for row in conn.execute("SELECT t.assigned_to AS who, COUNT(t.target_met) AS n, SUM(t.target_met) AS ok" +
                            base.format(col="t.first_reply_at"), (provider_id, month)):
        if row["n"] >= 3 and row["ok"] == row["n"] and row["who"] in won:
            won[row["who"]].append(BADGES["on_time"])
    for key, who in picks.items():
        if who in won:
            won[who].append(BADGES[key])
    return won


def health(conn, company_id):
    """('good' | 'watch' | 'bad', [reasons]) for one client company."""
    now = _now().strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT SUM(status = 'open') AS open,"
        " SUM(status = 'open' AND target_met IS NULL AND due_at < ?) AS late,"
        " SUM(status = 'open' AND priority = 'urgent') AS urgent,"
        " SUM(rating = 1 AND resolved_at >= ?) AS unhappy"
        " FROM tickets WHERE company_id = ?",
        (now, (_now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"), company_id)).fetchone()
    soon = (_now() + timedelta(days=30)).strftime(DAY)
    ending = conn.execute("SELECT COUNT(*) FROM devices WHERE company_id = ? AND warranty_end != ''"
                          " AND warranty_end <= ?", (company_id, soon)).fetchone()[0]
    bad, watch = [], []
    if row["late"]:
        bad.append(f"{row['late']} late repl{'ies' if row['late'] > 1 else 'y'}")
    if row["urgent"]:
        bad.append(f"{row['urgent']} urgent open")
    if (row["open"] or 0) > 2:
        watch.append(f"{row['open']} open tickets")
    elif row["open"]:
        watch.append(f"{row['open']} open ticket{'s' if row['open'] > 1 else ''}")
    if ending:
        watch.append(f"{ending} warrant{'ies' if ending > 1 else 'y'} ending")
    if row["unhappy"]:
        watch.append("unhappy rating lately")
    if bad:
        return "bad", bad + watch
    if (row["open"] or 0) > 2 or ending or row["unhappy"]:
        return "watch", watch
    return "good", watch or ["All calm"]
