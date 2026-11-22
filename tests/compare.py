"""與標準答案比對：整數須相同，小數容許 ±0.01；略過 generated_at、snapshot_id、health、src。"""
SKIP = {"generated_at", "snapshot_id", "health", "src", "_說明"}


def diff(got, exp, path="", tol=0.01):
    out = []
    if isinstance(exp, dict):
        if not isinstance(got, dict):
            return [f"{path}: 型別 {type(got).__name__} ≠ dict"]
        for k, v in exp.items():
            if k in SKIP:
                continue
            if k not in got:
                out.append(f"{path}.{k}: 缺")
            else:
                out += diff(got[k], v, f"{path}.{k}", tol)
    elif isinstance(exp, list):
        if not isinstance(got, list) or len(got) != len(exp):
            out.append(f"{path}: 長度/型別不符 got={got!r} exp={exp!r}")
        else:
            for i, (a, b) in enumerate(zip(got, exp)):
                out += diff(a, b, f"{path}[{i}]", tol)
    elif isinstance(exp, float) and not isinstance(exp, bool):
        if not isinstance(got, (int, float)) or abs(got - exp) > tol + 1e-9:
            out.append(f"{path}: {got!r} ≠ {exp!r}")
    else:
        if got != exp:
            out.append(f"{path}: {got!r} ≠ {exp!r}")
    return out
