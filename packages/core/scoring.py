def weighted_overall(rubric: list[dict], rubric_scores: list) -> float:
    weighted_sum = 0.0
    total_weight = 0.0
    for item in rubric:
        match = next((rs for rs in rubric_scores if rs.dimension == item["dimension"]), None)
        if match is None:
            continue
        weighted_sum += match.score * item["weight"]
        total_weight += item["weight"]
    if total_weight == 0:
        return 0.0
    return round(weighted_sum / total_weight, 4)
