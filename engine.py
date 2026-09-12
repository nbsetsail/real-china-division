# -*- coding: utf-8 -*-
"""
区划解析引擎 v1（不留存架构）
====================================
相对 v0 的升级：
  - 数据底座接入真实官方数据（中国·国家地名信息库，33省/341地级/2847县级，带12位官方码）
  - 解析结果输出官方 12 位区划码（双口径的统计侧映射待村级两码渠道摸底后接入）
  - 其余原则不变：无状态、置信度分级、诚实拒绝、歧义输出候选

数据优先级：data/divisions_v1.json（真实库）> 内置迷你库（离线兜底）
用法：
  python pipeline_skeleton.py                                  # 演示
  python pipeline_skeleton.py --test golden_test_set_v0.json   # 黄金测试集回归
"""
import json
import re
import sys
from pathlib import Path

REAL_DATA = Path(__file__).parent / "data" / "divisions_v1.json"
TOWNSHIP_DATA = Path(__file__).parent / "data" / "townships_v1.json"
ALIAS_FILE = Path(__file__).parent / "aliases.json"

# ---------------------------------------------------------------------------
# 迷你兜底库（真实数据不可用时使用）
# ---------------------------------------------------------------------------
FALLBACK = {
    "北京市": {"code": "110000000000", "cities": {"": {"code": None, "counties": {
        "东城区": "110101000000", "朝阳区": "110105000000", "海淀区": "110108000000", "通州区": "110112000000"}}}},
    "上海市": {"code": "310000000000", "cities": {"": {"code": None, "counties": {
        "黄浦区": "310101000000", "浦东新区": "310115000000", "崇明区": "310151000000"}}}},
    "浙江省": {"code": "330000000000", "cities": {"杭州市": {"code": "330100000000", "counties": {
        "西湖区": "330106000000", "余杭区": "330110000000"}}}},
}

# 别名词典：优先从 aliases.json 加载（数据文件化：可版本化/可开源/可扩量），否则用内置兜底
ALIAS = {
    "襄樊": "湖北省襄阳市", "徽州地区": "安徽省黄山市", "徽州": "安徽省黄山市",
    "郫县": "四川省成都市郫都区", "巢湖市": "安徽省合肥市", "莱芜": "山东省济南市",
    "满城县": "河北省保定市满城区", "即墨市": "山东省青岛市即墨区",
    "崇明县": "上海市崇明区", "雄安新区": "河北省保定市",
    "汴梁": "河南省开封市", "汴京": "河南省开封市", "燕京": "北京市",
    "华强北": "广东省深圳市福田区", "中关村": "北京市海淀区",
    "陆家嘴": "上海市浦东新区", "横店": "浙江省金华市东阳市",
    "白沟": "河北省保定市高碑店市",
    "阿拉善盟": "内蒙古自治区阿拉善盟", "延边朝鲜族自治州": "吉林省延边朝鲜族自治州",
    "杭洲": "杭州", "山东拾": "山东省",
    "沪": "上海市", "京": "北京市", "姑苏": "江苏省苏州市姑苏区",
}

AMBIG_EXTRA = {
    "长安": ["陕西省-西安市-长安区", "陕西省-西安市(古称)"],
    "朝阳": ["北京市-朝阳区", "辽宁省-朝阳市"],
}
MULTI_REGIONS = {"深莞惠": ["广东省-深圳市", "广东省-东莞市", "广东省-惠州市"]}
UNRESOLVABLE_WORDS = ["苏北", "苏南", "珠三角", "长三角", "某某"]
SPECIAL_DIRECT = {
    "西咸新区": ("陕西省-西安市/咸阳市", "国家级新区，非民政正式建制"),
    "苏州工业园区": ("江苏省-苏州市", "园区非正式建制"),
    "高新技术产业开发区": ("需按上级城市落位", "开发区非正式建制"),
    "成都高新": ("四川省-成都市", "口语+非正式区划"),
    "郑州航空港": ("河南省-郑州市", "实验区，统计口径有专项代码"),
    "生产建设兵团": ("新疆维吾尔自治区-兵团", "兵团为统计口径特殊建制"),
    "济源市": ("河南省-济源市(省直辖县级市)", "省直辖县级市，双口径均特殊"),
}

ALIAS_META = {"version": "builtin", "entries": len(ALIAS)}
if ALIAS_FILE.exists():
    try:
        _adict = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
        _flat = {}
        for _key in ("historical", "landmarks", "simplified", "typo"):
            for _e in _adict.get(_key, []):
                _flat[_e["alias"]] = _e["canonical"]
        ALIAS = _flat
        AMBIG_EXTRA = {k: v for k, v in _adict.get("ambiguous", {}).items()}
        MULTI_REGIONS = {k: v for k, v in _adict.get("multi", {}).items()}
        UNRESOLVABLE_WORDS = list(_adict.get("unresolvable", UNRESOLVABLE_WORDS))
        SPECIAL_DIRECT = {k: tuple(v) for k, v in _adict.get("special", {}).items()}
        ALIAS_META = {"version": _adict.get("meta", {}).get("version"), "entries": len(ALIAS)}
    except Exception as e:
        print(f"[warn] aliases.json 加载失败，使用内置词典: {e}")


# ---------------------------------------------------------------------------
# 数据加载：真实库 > 兜底库；统一结构 {省: {"code", "cities": {市或"": {"code", "counties": {名: 码}}}}}
# ---------------------------------------------------------------------------
def load_divisions():
    if REAL_DATA.exists():
        raw = json.loads(REAL_DATA.read_text(encoding="utf-8"))
        provs = {}
        for p, pv in raw["provinces"].items():
            provs[p] = {"code": pv.get("code"),
                        "cities": {c: {"code": cv.get("code"), "counties": dict(cv.get("counties", {}))}
                                   for c, cv in pv.get("cities", {}).items()}}
        return provs, {"source": "real", "fetched_at": raw["meta"]["fetched_at"]}
    provs = {}
    for p, v in FALLBACK.items():
        cities = {}
        for c, cv in v["cities"].items():
            # 兜底库无独立码表时，县级码直接用值本身（真实库加载后不会走到这里）
            counties = cv["counties"]
            cities[c] = {"code": cv["code"], "counties": counties}
        provs[p] = {"code": v["code"], "cities": cities}
    return provs, {"source": "fallback-mini", "fetched_at": None}


DIV, DATA_META = load_divisions()

# ---------------------------------------------------------------------------
# 乡镇级（四级）：{县级码: {"county": 区县名, "townships": {乡名: 乡码}}}
# 另建全局索引 TOWNSHIP_GLOBAL（仅收录 >=3 字且带 街道/镇/乡 后缀的名字，防止单字/短名误伤）
# ---------------------------------------------------------------------------
COUNTY_TOWNSHIPS, TOWNSHIP_GLOBAL = {}, {}
if TOWNSHIP_DATA.exists():
    for ccode, cv in json.loads(TOWNSHIP_DATA.read_text(encoding="utf-8")).items():
        COUNTY_TOWNSHIPS[ccode] = cv["townships"]
        for tname in cv["townships"]:
            if len(tname) >= 3 and tname.endswith(("街道", "镇", "乡")):
                TOWNSHIP_GLOBAL.setdefault(tname, []).append(ccode)

# 县级码 -> (省, 市, 县) 路径反查（乡镇直配时组装完整路径）
COUNTY_PATH = {}
for _p, _pv in DIV.items():
    for _c, _cv in _pv["cities"].items():
        for _cn, _ccode in _cv["counties"].items():
            COUNTY_PATH[_ccode] = (_p, _c or None, _cn)

# ---------------------------------------------------------------------------
# 变更事件库（C2 地基）：历史名自动别名 + 旧码→新码映射（数据驱动，不再手工硬编码）
# 硬过滤：旧名不与现行区划名冲突、长度>=3 且带建制后缀（防"郊区"类泛名误伤输入）、
#         新码必须是现行库中的码（映射必须落在现行区划上）
# ---------------------------------------------------------------------------
EVENTS_FILE = Path(__file__).parent / "data" / "historical_changes_v1.json"
HIST_ALIAS, OLD_CODE_MAP, EVENTS_META, EVENTS = {}, {}, None, []
CODE_PATH = {}
for _p, _pv in DIV.items():
    CODE_PATH[_pv["code"]] = ([_p], {"province": _pv["code"]})
    for _c, _cv in _pv["cities"].items():
        if _c and _cv.get("code"):
            CODE_PATH[_cv["code"]] = ([_p, _c], {"province": _pv["code"], "city": _cv["code"]})
        for _cn, _ccode in _cv["counties"].items():
            _base = {"province": _pv["code"], "city": _cv.get("code") if _c else None, "county": _ccode}
            CODE_PATH[_ccode] = ([_p] + ([_c] if _c else []) + [_cn], _base)
            for _tn, _tcode in COUNTY_TOWNSHIPS.get(_ccode, {}).items():
                CODE_PATH[_tcode] = ([_p] + ([_c] if _c else []) + [_cn, _tn], dict(_base, township=_tcode))

CURRENT_CODES = set(CODE_PATH)
CURRENT_NAMES = {seg for path, _ in CODE_PATH.values() for seg in path}
KNOWN_NAMES = CURRENT_NAMES | {tn for _tv in COUNTY_TOWNSHIPS.values() for tn in _tv}
# 去后缀短形式（"宁波市"->"宁波"），供建制边界判断识别"城市名+县名"连写
KNOWN_SHORT = {n.rstrip("市区县旗盟") for n in KNOWN_NAMES if len(n.rstrip("市区县旗盟")) >= 2}
if EVENTS_FILE.exists():
    try:
        _edict = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        EVENTS_META = _edict["meta"]
        EVENTS = _edict["events"]
        for _e in _edict["events"]:
            _on, _oc = _e["old"]["name"], _e["old"]["code"]
            _nn, _nc = _e["new"]["name"], _e["new"]["code"]
            _np = _e.get("new_path") or []
            if _oc and _nc and _oc != _nc and _nc in CURRENT_CODES:
                OLD_CODE_MAP[_oc] = {"code": _nc, "path": _np, "year": _e["year"], "type": _e["type"]}
            if (_on and _np and len(_np) >= 2 and _nc in CURRENT_CODES and _e["type"] != "撤销"
                    and (_on.endswith(("县", "旗", "盟")) if len(_on) == 2
                         else _on.endswith(("县", "市", "区", "旗", "盟", "地区", "自治州", "自治县", "林区", "特区")))
                    and _on not in CURRENT_NAMES):
                HIST_ALIAS[_on] = "-".join(_np)
        EVENTS_META = dict(EVENTS_META, hist_alias=len(HIST_ALIAS), old_code_map=len(OLD_CODE_MAP))
    except Exception as e:
        print(f"[warn] historical_changes_v1.json 加载失败，历史映射降级为内置词典: {e}")
        EVENTS_META = None

ALIAS_ALL = {**HIST_ALIAS, **ALIAS}  # 手工词典优先于自动生成

# ---------------------------------------------------------------------------
# 拼音纠错（C3）：同音错字免规则命中（"深镇/山冬/杭洲"），替代 aliases.json 错字硬编码。
# pypinyin 为可选依赖：未安装时纠错降级关闭，主路径（规则+词典）不受影响。
# 索引惰性构建（首次纠错时），仅收录省/市/县三级约 6.4k 形态。
# ---------------------------------------------------------------------------
try:
    from pypinyin import lazy_pinyin
    _HAS_PY = True
except Exception:
    _HAS_PY = False

PY_INDEX = None


def _pinyin_key(s: str) -> str:
    return "".join(lazy_pinyin(s)).lower()


def _build_py_index():
    global PY_INDEX
    PY_INDEX = {}

    def add(name, path):
        for form in {name, name.rstrip("省市区县盟旗")}:
            if len(form) >= 2:
                PY_INDEX.setdefault(_pinyin_key(form), []).append((name, path))

    for _p, _pv in DIV.items():
        add(_p, [_p])
        for _c, _cv in _pv["cities"].items():
            if _c:
                add(_c, [_p, _c])
            for _cn in _cv["counties"]:
                add(_cn, [_p] + ([_c] if _c else []) + [_cn])
    return PY_INDEX


def _entries(cities: dict):
    """统一城市层：返回 [(市名或None, 市code, counties_dict), ...]。"""
    return [(c or None, cv.get("code"), cv["counties"]) for c, cv in cities.items()]


def _strip_noise(text: str) -> str:
    out = []
    for ch in text:
        if ch.isdigit() and out and out[-1].isdigit():
            continue
        out.append(ch)
    return "".join(out).replace(" ", "")


_SUFFIX_CHARS = "区县市旗盟镇乡村"


def _at_boundary(text: str, i: int) -> bool:
    """位置 i 是否处于建制边界：串首 / 前邻非汉字 / 前邻是边界字 / 前缀以已知区划名结尾。
    「宁波鄞州」的鄞州（前缀'宁波'是已知市）算边界，「胶南县」的南县（前缀'胶'）不算。"""
    if i == 0:
        return True
    prev = text[i - 1]
    if prev < "\u4e00" or prev in "省市区县盟旗州":
        return True
    return any(text[:i][-_l:] in KNOWN_NAMES or text[:i][-_l:] in KNOWN_SHORT
               for _l in (4, 3, 2))


def _apply_alias(text: str) -> str:
    # 防误伤三条：①别名后紧跟建制后缀且拼成已知区划名（如「沙县」+「区」->「沙县区」）时不替换；
    # ②别名必须位于建制边界——单字别名「京」不得命中「南京」词中（v0.3 回归教训）；
    # ③前邻是已知区划名结尾时视为边界（「宁波鄞州」的鄞州）
    for k in sorted(ALIAS_ALL, key=len, reverse=True):
        start = 0
        while True:
            i = text.find(k, start)
            if i < 0:
                break
            j = i + len(k)
            # 别名+下一字符拼成已知区划名（全名或去后缀短形式，"京山"->京山市）时不替换
            if text[j:j + 1] and (k + text[j]) in KNOWN_NAMES | KNOWN_SHORT:
                start = j
                continue
            if not _at_boundary(text, i):
                start = j
                continue
            text = text[:i] + ALIAS_ALL[k] + text[j:]
            start = i + len(ALIAS_ALL[k])
    return text


def _match_at(text: str, name: str, boundary2: bool = False):
    """同 _match，但返回 (命中形, 位置) 供位置优先消歧。"""
    for form in (name, name.rstrip("省市县区盟旗")):
        if form and len(form) >= 2:
            start = 0
            while True:
                i = text.find(form, start)
                if i < 0:
                    break
                if boundary2 and len(form) == 2 and not _at_boundary(text, i):
                    start = i + 1
                    continue
                return form, i
    return None


def _match(text: str, name: str, boundary2: bool = False):
    # 去后缀形式必须 >=2 字：单字子串匹配会严重误伤（"新县"->"新"命中"浦东新区"——v1 回归教训）。
    # boundary2：两字形式命中位置须处于建制边界（"胶南县"不得命中"南县"——v0.2 回归教训）
    r = _match_at(text, name, boundary2)
    return r[0] if r else None


def _fuzzy_in(text: str, name: str):
    """编辑距离 1 的错字命中（仅对 >=3 字词启用，防两字名误伤——v0 回归教训）。
    命中窗口须处于建制边界（"中山西路"里的"山西路"不得命中山西省——v0.4 回归教训）。
    返回 (命中的标准形, 命中窗口)，供拼音验证消歧（"山冬省"：山东/山西字符距离同为1，拼音分胜负）。"""
    for form in (name, name.rstrip("省市县区盟旗")):
        n = len(form)
        if n < 3:
            continue
        for i in range(len(text) - n + 1):
            win = text[i:i + n]
            if sum(a != b for a, b in zip(win, form)) == 1:
                if not _at_boundary(text, i):
                    continue
                return form, win
    return None


def _fmt(prov, city, county):
    parts = [prov] + ([city] if city else []) + ([county] if county else [])
    return "-".join(parts)


def _resolve_core(raw: str) -> dict:
    """核心解析：纯函数，无状态。返回 {status, result, codes, confidence, note}。"""
    text = _apply_alias(_strip_noise(raw))

    for w in UNRESOLVABLE_WORDS:
        if w in text:
            return {"status": "unresolvable", "result": None, "codes": None, "confidence": 0.0,
                    "note": f"区域泛称/占位符「{w}」不解析，诚实拒绝"}

    for key, (loc, why) in SPECIAL_DIRECT.items():
        if key in text:
            return {"status": "special", "result": loc, "codes": None, "confidence": 0.8,
                    "note": f"特殊口径：{why}"}

    if text in AMBIG_EXTRA:
        return {"status": "ambiguous", "result": AMBIG_EXTRA[text], "codes": None, "confidence": 0.9,
                "note": "别名/区名双关，输出候选集"}
    if text in MULTI_REGIONS:
        return {"status": "multi", "result": MULTI_REGIONS[text], "codes": None, "confidence": 0.9,
                "note": "区域合称拆分为多实体"}

    prov_found = []
    for p in DIV:
        m = _match(text, p, boundary2=True)  # 短形式须边界：防"中山西路"命中"山西"（v0.4 回归教训）
        if m:
            prov_found.append((p, 1.0 if m == p else 0.9, m))
        else:
            f = _fuzzy_in(text, p)
            if f:
                form, win = f
                conf = 0.8 if (not _HAS_PY or _pinyin_key(win) == _pinyin_key(form)) else 0.7
                prov_found.append((p, conf, form))

    if not prov_found:
        hits = _global_county_match(text)
        if hits is None:
            # 乡镇/街道名直配（仅收录 >=3 字且带后缀的名字，防误伤）；按后缀位置回溯截取候选，
            # 覆盖镇名嵌中间（"下沙街道6号大街452号"）与带市级前缀（"杭州市下沙街道"）两类
            t_name, t_codes = text, TOWNSHIP_GLOBAL.get(text, [])
            if not t_codes:
                cands = set()
                for m in re.finditer(r"街道|镇|乡", text):
                    end = m.end()
                    for k in range(max(0, end - 8), end - 1):
                        cands.add(text[k:end])
                for cand in sorted(cands, key=len, reverse=True):
                    if cand in TOWNSHIP_GLOBAL:
                        t_name, t_codes = cand, TOWNSHIP_GLOBAL[cand]
                        break
            if len(t_codes) == 1:
                ccode = t_codes[0]
                p, c, cn = COUNTY_PATH[ccode]
                return {"status": "resolve",
                        "result": _fmt(p, c, cn) + "-" + t_name,
                        "codes": {"province": DIV[p]["code"],
                                  "city": DIV[p]["cities"].get(c or "", {}).get("code") if c else None,
                                  "county": ccode, "township": COUNTY_TOWNSHIPS[ccode][t_name]},
                        "confidence": 0.9, "note": "乡镇/街道直配反推上级"}
            if len(t_codes) > 1:
                paths = sorted({_fmt(*COUNTY_PATH[cc]) for cc in t_codes})
                return {"status": "ambiguous", "result": paths, "codes": None, "confidence": 0.9,
                        "note": "同名乡镇/街道多处存在，输出候选集"}
            # 地级市直配补洞（无省级前缀："深圳""常州市"类；乡镇更具体已先行）
            city_hits = [(p, c) for p, pv in DIV.items() for c in pv["cities"]
                         if c and _match(text, c, boundary2=True)]
            if len(city_hits) == 1:
                p, c = city_hits[0]
                m = _match(text, c, boundary2=True)
                rest_after = text.replace(m, "", 1) if m else text
                if any(mk in rest_after for mk in ("开发区", "园区", "高新区", "新区")):
                    return {"status": "special", "result": _fmt(p, c, None),
                            "codes": {"province": DIV[p]["code"],
                                      "city": DIV[p]["cities"][c].get("code"), "county": None},
                            "confidence": 0.8,
                            "note": "含开发区/园区/新区表述，非正式区划，落到上级市并标注口径"}
                return {"status": "resolve", "result": _fmt(p, c, None),
                        "codes": {"province": DIV[p]["code"],
                                  "city": DIV[p]["cities"][c].get("code"), "county": None},
                        "confidence": 0.9, "note": "地级市直配（无省级前缀）"}
            if len(city_hits) > 1:
                return {"status": "ambiguous", "result": sorted(_fmt(p, c, None) for p, c in city_hits),
                        "codes": None, "confidence": 0.9, "note": "同名地级市多处存在，输出候选集"}
            return {"status": "unresolvable", "result": None, "codes": None, "confidence": 0.0,
                    "note": "未能识别任何区划实体"}
        refined = [h for h in hits if any(seg.rstrip("市") in text for seg in h["path"][:-1])]
        hits = refined or hits
        if len(hits) > 1 and len({h["pos"] for h in hits}) > 1:
            # 多县命中但位置不同：地址语序靠前者胜出（"昆山开发区前进东路"——昆山在句首，前进区在佳木斯）
            hits = [min(hits, key=lambda h: h["pos"])]
        if len(hits) == 1:
            h = hits[0]
            result_str, hcodes = "-".join(h["path"]), dict(h["codes"])
            ccode = hcodes.get("county")
            for tn in COUNTY_TOWNSHIPS.get(ccode or "", {}):
                if _match(text, tn):
                    result_str += "-" + tn
                    hcodes["township"] = COUNTY_TOWNSHIPS[ccode][tn]
                    break
            return {"status": "resolve", "result": result_str, "codes": hcodes,
                    "confidence": 1.0, "note": "区县直配反推上级"}
        return {"status": "ambiguous", "result": ["-".join(h["path"]) for h in hits], "codes": None,
                "confidence": 0.9, "note": "同名区县多处存在，输出候选集"}

    best_p, conf, best_m = max(prov_found, key=lambda x: (x[1], len(x[2])))
    rest = text.replace(best_m, "", 1)
    entry = DIV[best_p]
    entries = _entries(entry["cities"])

    hit_city, city_code, county_pool = None, None, []
    for c, ccode, counties in entries:
        if c:
            m = _match(rest, c, boundary2=True)
            if m:
                hit_city, city_code, county_pool = c, ccode, counties
                rest = rest.replace(m, "", 1)
                break
    if hit_city is None:
        county_pool = [cn for _, _, counties in entries for cn in counties]

    county_hits = [cn for cn in county_pool if _match(rest, cn)]
    codes = {"province": entry["code"], "city": city_code, "county": None}

    if len(county_hits) == 1:
        cn = county_hits[0]
        # 直辖市等无地级层：counties 挂在 "" 键下
        holder = DIV[best_p]["cities"].get(hit_city or "")
        ccode = holder["counties"].get(cn) if holder else None
        codes["county"] = ccode
        # 四级：在全县乡镇表中找（别名/地标解析后 rest 含原始文本，乡名直接匹配）
        township = None
        for tn in COUNTY_TOWNSHIPS.get(ccode or "", {}):
            if _match(rest.replace(cn, "", 1), tn):
                township = tn
                break
        if township:
            codes = dict(codes, township=COUNTY_TOWNSHIPS[ccode][township])
            result_str = _fmt(best_p, hit_city, cn) + "-" + township
            return {"status": "resolve", "result": result_str, "codes": codes,
                    "confidence": max(conf, 0.9), "note": "四级解析（省市区县+乡镇街道）"}
        if any(mk in rest for mk in ("开发区", "园区", "高新区")):
            return {"status": "special", "result": _fmt(best_p, hit_city, cn), "codes": codes,
                    "confidence": 0.8, "note": "含开发区/园区表述，口径已标注"}
        return {"status": "resolve", "result": _fmt(best_p, hit_city, cn), "codes": codes,
                "confidence": conf, "note": "逐级匹配"}

    if len(county_hits) > 1:
        return {"status": "ambiguous", "result": [_fmt(best_p, hit_city, cn) for cn in county_hits],
                "codes": None, "confidence": 0.9, "note": "同名区县歧义，输出候选集"}

    # 县级未命中 -> 本市范围内乡镇/街道直配兜底（"杭州市下沙街道"类，v0.2 补洞）
    if hit_city and isinstance(county_pool, dict):
        _in_city = [tc for tc in TOWNSHIP_GLOBAL.get(rest, []) if tc in county_pool.values()]
        if len(_in_city) == 1:
            _cc = _in_city[0]
            return {"status": "resolve",
                    "result": _fmt(best_p, hit_city, COUNTY_PATH[_cc][2]) + "-" + rest,
                    "codes": {"province": entry["code"], "city": city_code,
                              "county": _cc, "township": COUNTY_TOWNSHIPS[_cc][rest]},
                    "confidence": 0.9, "note": "市内乡镇/街道直配（县级未命中兜底）"}
        if len(_in_city) > 1:
            return {"status": "ambiguous",
                    "result": sorted({_fmt(best_p, hit_city, COUNTY_PATH[tc][2]) for tc in _in_city}),
                    "codes": None, "confidence": 0.9, "note": "本市同名乡镇/街道，输出候选集"}

    if any(mk in rest for mk in ("开发区", "园区", "高新区", "新区")) and best_p not in ("上海市", "天津市", "北京市", "重庆市"):
        return {"status": "special", "result": _fmt(best_p, hit_city, None), "codes": codes,
                "confidence": 0.8, "note": "非正式区划（开发区/园区等），落到上级并标注口径"}
    if hit_city:
        return {"status": "resolve", "result": _fmt(best_p, hit_city, None), "codes": codes,
                "confidence": 0.9, "note": "解析到地级市"}
    if conf < 0.8:
        # 未过拼音验证的模糊省匹配不单独作数，退回全库区县直配取证（"南京市鼓楼区"不得判成北京市——v0.4 回归教训）
        hits2 = _global_county_match(text)
        if hits2:
            refined2 = [h for h in hits2 if any(seg.rstrip("市") in text for seg in h["path"][:-1])]
            hits2 = refined2 or hits2
            if len(hits2) > 1 and len({h["pos"] for h in hits2}) > 1:
                hits2 = [min(hits2, key=lambda h: h["pos"])]
            if len(hits2) == 1:
                h = hits2[0]
                result_str, hcodes = "-".join(h["path"]), dict(h["codes"])
                ccode = hcodes.get("county")
                for tn in COUNTY_TOWNSHIPS.get(ccode or "", {}):
                    if _match(text, tn):
                        result_str += "-" + tn
                        hcodes["township"] = COUNTY_TOWNSHIPS[ccode][tn]
                        break
                return {"status": "resolve", "result": result_str, "codes": hcodes,
                        "confidence": 0.8, "note": "模糊省匹配被县级证据修正"}
            return {"status": "ambiguous", "result": ["-".join(h["path"]) for h in hits2],
                    "codes": None, "confidence": 0.8, "note": "模糊省匹配且多县证据，输出候选集"}
    return {"status": "resolve", "result": _fmt(best_p, None, None), "codes": codes,
            "confidence": conf, "note": "解析到省级"}


def _global_county_match(text: str):
    """省未命中时，全库直配区县；返回 [{"path": [...], "codes": {...}, "pos": 命中位置}] 或 None。
    两字名启用边界校验（boundary2），防「胶南县」误命中「南县」。"""
    hits = []
    for p, pv in DIV.items():
        for c, cv in pv["cities"].items():
            for cn in cv["counties"]:
                r = _match_at(text, cn, boundary2=True)
                if r:
                    hits.append({"path": [p] + ([c] if c else []) + [cn],
                                 "codes": {"province": pv["code"], "city": cv.get("code") if c else None,
                                           "county": cv["counties"][cn]}, "pos": r[1]})
    return hits or None


def resolve(raw: str) -> dict:
    """对外入口：核心解析 + （unresolvable/ambiguous 时）一次拼音纠错重试。
    纠错采纳规则：unresolvable 态接受任何有效解析；ambiguous 态仅当纠错后变为唯一解析
    才采纳（"通州区"类真歧义不被破坏，"杭洲市西湖区"类错字歧义被修复）。"""
    out = _resolve_core(raw)
    if _HAS_PY and raw and raw.strip() and out["status"] in ("unresolvable", "ambiguous"):
        try:
            hit = _pinyin_retry(raw.strip())
        except Exception:
            hit = None
        if hit:
            out2, seg, name = hit
            # unresolvable 态：任何有效解析都可采纳；ambiguous 态：仅长段(>=3字)纠正出唯一解析才采纳
            adopt = (out2["status"] != "unresolvable") if out["status"] == "unresolvable" \
                else (out2["status"] == "resolve" and len(seg) >= 3)
            if adopt:
                out2["confidence"] = min(out2.get("confidence") or 0.8, 0.8)
                out2["note"] = f"拼音纠错「{seg}」→「{name}」；{out2['note']}"
                return out2
    return out


def _pinyin_retry(text: str):
    """滑窗取 2-6 字段做全拼匹配（同音不同字），长段优先；候选需重解析非 unresolvable 才采纳。"""
    if PY_INDEX is None:
        _build_py_index()
    for n in (6, 5, 4, 3, 2):
        for i in range(0, len(text) - n + 1):
            seg = text[i:i + n]
            for name, path in PY_INDEX.get(_pinyin_key(seg), []):
                # 输入段已是该实体的去后缀短名（"朝阳"→朝阳市/县/区）不算错字，跳过——防破坏真歧义
                if name == seg or name.rstrip("省市区县盟旗") == seg:
                    continue
                out2 = _resolve_core(text[:i] + name + text[i + n:])
                if out2["status"] != "unresolvable":
                    return out2, seg, name
    return None


def resolve_any(text: str) -> dict:
    """统一入口：非数字字符仅为空白/连字符时走编码直查，否则走地址解析。
    （"上海市 200000"含汉字仍是地址；"510124"/"110000000000"是编码）API 服务端用。"""
    t = text.strip()
    if t and not (set(t) - set("0123456789 -")):
        return resolve_by_code(t)
    return resolve(t)


def resolve_by_code(code: str) -> dict:
    """编码直查：现行 12 位/6 位码 -> 路径；历史码 -> 经变更事件库映射到现行区划。
    L1 API「编码 / 版本回溯」端点的核心函数，纯函数无状态。"""
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if len(digits) == 6:
        digits += "000000"
    if len(digits) != 12:
        return {"status": "unresolvable", "result": None, "codes": None, "confidence": 0.0,
                "note": "区划码需 6 位或 12 位数字"}
    if digits in CODE_PATH:
        path, codes = CODE_PATH[digits]
        return {"status": "resolve", "result": "-".join(path), "codes": dict(codes),
                "confidence": 1.0, "note": "编码直查（现行区划）"}
    cur, hops = digits, []
    while cur in OLD_CODE_MAP and len(hops) < 6:
        hops.append(OLD_CODE_MAP[cur])
        cur = OLD_CODE_MAP[cur]["code"]
        if cur in CODE_PATH:
            path, codes = CODE_PATH[cur]
            first = hops[0]
            return {"status": "historical", "result": "-".join(path),
                    "codes": dict(codes, old_code=digits, current_code=cur),
                    "confidence": 0.9,
                    "note": f"历史码映射：{first['year']} 年{first['type']}，映射链 {len(hops)} 跳"}
    return {"status": "unresolvable", "result": None, "codes": None, "confidence": 0.0,
            "note": "未能识别的区划码（现行与历史库均无）"}


def query_events(year=None, q=None, code=None, limit=50):
    """变更事件库查询（L1 版本查询端点核心）：按年份/关键词/区划码过滤，纯函数。
    code 支持 6/12 位（旧码新码均可，自动补零）；q 对事件全文字段做子串匹配。"""
    year = int(year) if year else None
    c = None
    if code:
        d = "".join(ch for ch in str(code) if ch.isdigit())
        c = d + "000000" if len(d) == 6 else d
        if len(c) != 12:
            c = None
    blobs = getattr(query_events, "_blobs", None)
    if blobs is None:
        blobs = query_events._blobs = [json.dumps(e, ensure_ascii=False) for e in EVENTS]
    out = []
    for e, blob in zip(EVENTS, blobs):
        if year and e["year"] != year:
            continue
        if c and e["old"]["code"] != c and e["new"]["code"] != c:
            continue
        if q and q not in blob:
            continue
        out.append({"year": e["year"], "type": e["type"], "level": e["level"],
                    "old": {"name": e["old"]["name"], "code": e["old"]["code"]},
                    "new": {"name": e["new"]["name"], "code": e["new"]["code"]},
                    "old_path": e.get("old_path"), "new_path": e.get("new_path"),
                    "doc_no": e.get("doc_no"), "source_url": e.get("source_url")})
    out.sort(key=lambda x: -x["year"])
    return {"total": len(out), "returned": min(len(out), limit), "events": out[:limit],
            "filter": {"year": year, "q": q, "code": c},
            "note": "变更事件库 1980-2026（官方代码簿差分+策展）；doc_no 增量回填中"}


def dual_code(query: str) -> dict:
    """双码对照（L1 端点核心）：民政口径现行码 × 统计口径的对照与差异点诚实披露。"""
    t = (query or "").strip()
    if not t:
        return {"status": "unresolvable", "note": "参数不能为空"}
    if set(t) - set("0123456789 -"):
        base = resolve(t)
    else:
        base = resolve_by_code(t)
    if base["status"] == "unresolvable":
        return {"status": "unresolvable", "query": t, "note": base["note"]}
    codes = dict(base.get("codes") or {})
    mca = {"口径": "民政部·国家地名信息库（年度版 2025-12-31 + 年内日更新）"}
    for k in ("province", "city", "county", "township"):
        if codes.get(k):
            mca[k] = codes[k]
    nbs = {
        "省市县乡四级": "统计用区划代码与民政 12 位码同源 GB/T 2260，四级一致（同源性抽查核验持续中）",
        "村级": "统计村级 12 位码与城乡分类代码自 2024-10 起无现行公开渠道（2023 版为最后一期）；"
               "本库村级实体以民政地名口径 20 位码提供，不做伪造映射",
        "开发区专项段": "统计口径存在开发区专项代码段，民政口径无此建制——引擎按特殊口径标注",
    }
    out = {"status": base["status"], "result": base.get("result"),
           "mca": mca, "nbs": nbs, "note": base.get("note")}
    if base["status"] == "historical":
        out["historical"] = {"old_code": codes.get("old_code"), "current_code": codes.get("current_code")}
    return out


def run_golden(path: str):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cases, by_type, passed = data["cases"], {}, 0
    for case in cases:
        exp, out = case["expect"], (resolve_by_code(case["code"]) if case.get("code") else resolve(case["raw"]))
        ok = _judge(exp, out)
        passed += ok
        by_type.setdefault(case["type"], [0, 0])
        by_type[case["type"]][0] += ok
        by_type[case["type"]][1] += 1
        shown = case.get("code") or case["raw"]
        print(f"[{'PASS' if ok else 'FAIL'}] {case['id']} {shown!r} -> {out['status']}:{out['result']}")
    print("\n===== 报告 =====")
    for t, (p, n) in by_type.items():
        print(f"  {t}: {p}/{n}")
    print(f"  总计: {passed}/{len(cases)}   数据源: {DATA_META['source']} (fetched {DATA_META['fetched_at']})")


def _judge(exp, out) -> bool:
    mode = exp.get("mode")
    if mode == "resolve":
        if out["status"] not in ("resolve", "special"):
            return False
        want = [exp[k] for k in ("province", "city", "county") if exp.get(k)]
        got = str(out["result"] or "")
        return all(any(part in seg for seg in got.split("-")) for part in want)
    if mode == "ambiguous":
        if out["status"] != "ambiguous":
            return False
        got = [str(g).replace("-", "") for g in (out["result"] or [])]
        return any(any(cand.replace("-", "") in g or g in cand.replace("-", "") for g in got)
                   for cand in exp.get("candidates", []))
    if mode == "multi":
        return out["status"] == "multi"
    if mode == "unresolvable":
        return out["status"] == "unresolvable"
    if mode == "code":
        if out["status"] != exp.get("status"):
            return False
        contains = exp.get("contains") or []
        return all(c in str(out["result"] or "") for c in contains)
    if mode == "special":
        return out["status"] in ("special", "resolve")
    return False


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--test":
        run_golden(sys.argv[2])
    else:
        for demo in ["杭洲市西湖区", "通州区", "汴梁", "深圳市南山区科技园", "合肥市庐江县", "雄安新区容城县", "苏北",
                     "襄樊市樊城区", "郫县犀浦镇", "宣化县", "忻州地区原平市"]:
            print(f"{demo!r} -> {resolve(demo)}")
        for cdemo in ["510124", "372622", "110000000000", "500157000000", "999999"]:
            print(f"code {cdemo!r} -> {resolve_by_code(cdemo)}")
        if EVENTS_META:
            print(f"变更事件库: {EVENTS_META['counts']['total']} 条事件，"
                  f"自动历史别名 {EVENTS_META['hist_alias']} 条，旧码映射 {EVENTS_META['old_code_map']} 条")
