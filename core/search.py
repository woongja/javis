"""Format local worklog search replies for the CLI and Telegram."""

from worklog import SearchStatus, Store


def search_reply(store: Store, query: str, usage: str) -> tuple[SearchStatus, str]:
    status, results = store.search(query)
    if status == "usage":
        return status, usage
    if status == "unavailable":
        return status, "검색 사용 불가"

    lines = [f"{row.id}\t{row.date}\t{row.preview}" for row in results] or ["일치 없음"]
    if status == "degraded":
        lines.append("조사 붙은 단어 부분 일치 제한")
    lines.append('출처는 `javis ask "출처 <ID>"`')
    return status, "\n".join(lines)
