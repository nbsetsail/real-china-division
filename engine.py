# -*- coding: utf-8 -*-
"""
区划解析引擎 v1（不留存架构）
====================================
相对 v0 的升级：
  - 数据底座接入真实官方数据（中国·国家地名信息库，34省级/341地级/2847县级，带12位官方码；台湾省按 GB/T 2260 补录）
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
from collections import Counter, defaultdict
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
# 区域泛称（大区/方位/古区域）：非行政区划。仅**整串精确匹配**时拒绝，不用子串匹配，
# 否则会误伤"华南路""东北街"这类真实地名（v1 校准集回归教训：华南曾因同音"桦南县"被误解析）。
REGION_GENERIC = {
    "华北", "华南", "华东", "华西", "华中", "东北", "西北", "西南", "东南",
    "中原", "江南", "江淮", "关中", "塞北", "岭南", "京津冀", "长三角", "珠三角",
    # 2026-09-17 补：方位泛称此前漏收，会被形近候选集捡成县名
    # （「北方」→北屯市、「西部」→西陵区——校准集 C013 回归教训）
    "北方", "南方", "东方", "西方", "东部", "西部", "南部", "北部", "中部",
    "沿海", "边疆", "内陆", "内地", "中国北方", "中国南方",
}
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
# 不构成"沿革"、因此既不参与同名歧义统计也不生成历史别名的事件类型：
#   码位改派 = 同一码在不同年份指向不同县（年度集合差分的误判产物，见 scripts/repair_events.py）
CODE_REASSIGN = "码位改派"
HIST_ALIAS, OLD_CODE_MAP, EVENTS_META, EVENTS = {}, {}, None, []
_ABOLISHED = {}   # 撤销迁移索引：{省名: {旧县名: {path, year, code, name}}}（B-26）
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
# 名称 -> 现行路径索引（同名歧义标 None）；供语料入库事件（无 new_path/无码）反查现行路径
NAME_PATH = {}
for _pn, _ in CODE_PATH.values():
    _nm = _pn[-1]
    NAME_PATH[_nm] = _pn if _nm not in NAME_PATH else None
# 通用通名：多城共有（如"东区/郊区/城区/市郊区/市中区"），单独作别名键会跨城误伤（v0.5 回归教训：
# "嘉兴市郊区"曾命中梧州"市郊区"的历史映射），必须用上级城市名限定。
GENERIC_TAILS = {"郊区", "城区", "市区", "矿区", "新区", "中区", "东区", "西区", "南区", "北区"}
KNOWN_NAMES = CURRENT_NAMES | {tn for _tv in COUNTY_TOWNSHIPS.values() for tn in _tv}
# 去后缀短形式（"宁波市"->"宁波"），供建制边界判断识别"城市名+县名"连写
KNOWN_SHORT = {n.rstrip("市区县旗盟") for n in KNOWN_NAMES if len(n.rstrip("市区县旗盟")) >= 2}
# 省名前缀（民政部语料入库事件的 old.name 带省名，如"浙江省嘉兴市郊区"，须剥离后作别名键）
PROV_FULL = set(DIV.keys())
PROV_SHORT = {_p.rstrip("省自治区") for _p in PROV_FULL if len(_p.rstrip("省自治区")) >= 2}
# 现行"省 / 地级市"名（含去后缀短形）：用于判断别名替换处的前文是否**已定位到现行区划**
PROV_ANY = PROV_FULL | PROV_SHORT
CITY_ANY = {_c for _pv in DIV.values() for _c in _pv["cities"] if _c}
CITY_ANY |= {_s for _c in CITY_ANY for _s in (_c.rstrip("市"),) if len(_s) >= 2}
# 省/市/县三级名字（含去后缀短形式）：专供"前邻是否为已知区划名结尾"的**边界判断**。
# 刻意**不含乡镇名**：乡镇名有 3.8 万条，2-3 字的镇名极易与其它词的尾部巧合相同——
# "…镇安县"里的"洛市镇"恰好是江西丰城的一个镇，会跨词命中并把该处误判为边界，
# 进而触发别名误替换（v3 交付演练回归教训）。
UPPER_NAMES = set()
for _p2, _pv2 in DIV.items():
    UPPER_NAMES.add(_p2)
    for _c2, _cv2 in _pv2["cities"].items():
        if _c2:
            UPPER_NAMES.add(_c2)
        UPPER_NAMES |= set(_cv2.get("counties") or {})
UPPER_NAMES |= {_s2 for _n2 in list(UPPER_NAMES)
                for _s2 in (_n2.rstrip("省市区县旗盟州"),) if len(_s2) >= 2}
_OLD_NAME_TARGETS = defaultdict(set)  # old.name -> 该旧称在事件库中指向的现行路径集合（>1 即同名歧义）


def _strip_prov_prefix(name: str) -> str:
    """剥离 old.name 冗余的省名前缀（"浙江省嘉兴市郊区" -> "嘉兴市郊区"）。
    剩余长度 <3 时不剥离，避免把"吉林市"这类与省同名的区划剥坏。"""
    for _pref in sorted(PROV_FULL | PROV_SHORT, key=len, reverse=True):
        if name.startswith(_pref) and len(name) - len(_pref) >= 3:
            return name[len(_pref):]
    return name


def _city_key(city_seg: str, name: str) -> str:
    """城市限定别名键；去重重复的"市"字（"梧州市" + "市郊区" -> "梧州市郊区"）。"""
    if city_seg.endswith("市") and name.startswith("市"):
        return city_seg[:-1] + name
    return city_seg + name


def _is_generic(name: str) -> bool:
    """通用通名判定：本身即通名（"郊区"），或三字且尾部为通名（"市郊区/铁西区/桥东区"）。"""
    return name in GENERIC_TAILS or (len(name) == 3 and name[-2:] in GENERIC_TAILS)


if EVENTS_FILE.exists():
    try:
        _edict = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        EVENTS_META = _edict["meta"]
        EVENTS = _edict["events"]
        # B-28 链式追尾索引：old.code -> 后续事件的 new.code（每码取第一条，确定性）
        _next_by_old = {}
        for _e2 in _edict["events"]:
            _oc2 = (_e2.get("old") or {}).get("code")
            _nc2 = (_e2.get("new") or {}).get("code")
            if _oc2 and _nc2 and _oc2 != _nc2 and _oc2 not in _next_by_old:
                _next_by_old[_oc2] = _nc2

        def _chain_to_current(code):
            """沿 old.code -> new.code 事件链追到现行码（≤6 跳，防环）。"""
            _seen = set()
            while code and code not in CURRENT_CODES and code not in _seen:
                _seen.add(code)
                code = _next_by_old.get(code)
            return code if code in CURRENT_CODES else None
        # 先扫描：同一旧称指向多个不同"终态"现行路径 => 该名本身有歧义，别名必须城市限定。
        # 只统计终态（末段仍在现行库中）：否则"宣化县→宣化县→宣化区"这类换码链会被误判为歧义。
        for _e in _edict["events"]:
            # 码位改派（同一码在不同年份指向不同县，由逐年集合差分误判为"更名"而来）不构成沿革，
            # 不参与同名歧义统计，也不生成别名——否则会产出错误的历史别名
            # （"芳村区→广州天河区"、"阿城县→大庆杜尔伯特蒙古族自治县"）。
            if _e.get("type") == CODE_REASSIGN:
                continue
            _s_on = _strip_prov_prefix((_e["old"] or {}).get("name") or "")
            _s_np = _e.get("new_path") or []
            if _s_on and _s_np and _s_np[-1] in CURRENT_NAMES:
                _OLD_NAME_TARGETS[_s_on].add("-".join(_s_np))
        for _e in _edict["events"]:
            if _e.get("type") == CODE_REASSIGN:
                continue
            _on, _oc = _e["old"]["name"], _e["old"]["code"]
            _nn, _nc = _e["new"]["name"], _e["new"]["code"]
            _op = _e.get("old_path") or []
            _np = _e.get("new_path") or []
            _is_current = _nc in CURRENT_CODES
            if not _is_current and _nn in NAME_PATH and NAME_PATH[_nn]:
                _np = NAME_PATH[_nn]  # 语料入库事件无码/无路径：反查现行路径
                _is_current = True
            elif not _is_current and _oc and _nc:
                # B-28（2026-09-17）链式追尾：new 指向已不存在的中间建制
                # （昌潍地区→潍坊地区→潍坊市）时沿事件链追到现行码——
                # 否则两跳沿革链整段被 _is_current 守卫丢弃。防环 ≤6 跳。
                _c2 = _chain_to_current(_nc)
                if _c2 and _c2 in CODE_PATH:
                    _nc, _np = _c2, list(CODE_PATH[_c2][0])
                    _nn = _np[-1]
                    _is_current = True
            elif len(_np) == 1 and _is_current and NAME_PATH.get(_np[0]):
                # 语料 new_path 只给末段、缺省/市（如"万县区→万州区"的 new_path=['万州区']）：
                # 按现行库补全省/市。否则会被下方 len(_np)>=2 的长度守卫整体丢弃（v3 校准集回归教训）。
                _np = NAME_PATH[_np[0]]
            if _oc and _nc and _oc != _nc and _nc in CURRENT_CODES:
                OLD_CODE_MAP[_oc] = {"code": _nc, "path": _np, "year": _e["year"], "type": _e["type"]}
                if _e["type"] == "撤销" and len(_np) >= 2:
                    _ABOLISHED.setdefault(_np[0], {})[(_e["old"] or {}).get("name") or ""] = {
                        "path": _np, "year": _e["year"], "code": _nc, "name": _np[-1]}
            _on = _strip_prov_prefix(_on or "")
            # 撤销类既往一律排除（612 条中 610 条年度差分产物 old 残缺）。B-25（2026-09-17）
            # 起对人工策展的成对撤销事件放开（当前仅渝北区/江北区→两江新区 2 条）：
            # 旧区名 → 新归属，支撑「旧地址自动迁移到新区划」。残缺撤销仍被 _is_current 等守卫排除。
            if (_on and _np and len(_np) >= 2 and _is_current
                    and (_e["type"] != "撤销" or _oc)
                    and (_on.endswith(("县", "旗", "盟")) if len(_on) == 2
                         else _on.endswith(("县", "市", "区", "旗", "盟", "地区", "自治州", "自治县", "林区", "特区")))
                    and _on not in CURRENT_NAMES
                    # 同名改码跨省 = 区划码被回收再分配给异地，不是沿革，禁止生成历史别名
                    and not (_e["type"] == "同名改码" and len(_op) >= 2 and len(_np) >= 2
                             and _op[0] != _np[0])):
                if _is_generic(_on) or len(_OLD_NAME_TARGETS.get(_on, ())) > 1:
                    # 通用名 / 同名歧义：用上级城市名限定（"邵阳市东区"、"嘉兴市郊区"）
                    _key = _city_key(_op[-2], _on) if len(_op) >= 2 else None
                else:
                    _key = _on
                if _key:
                    HIST_ALIAS[_key] = "-".join(_np)
        EVENTS_META = dict(EVENTS_META, hist_alias=len(HIST_ALIAS), old_code_map=len(OLD_CODE_MAP))
    except Exception as e:
        print(f"[warn] historical_changes_v1.json 加载失败，历史映射降级为内置词典: {e}")
        EVENTS_META = None

# B-30：撤销迁移旧名反查索引 {旧县名: [{prov, path, year, code, name}]}。
_ABOLISHED_BY_NAME = {}
for _prov, _names in _ABOLISHED.items():
    for _nm, _mig in _names.items():
        if _nm:
            _ABOLISHED_BY_NAME.setdefault(_nm, []).append(dict(_mig, prov=_prov))

# ---------------------------------------------------------------------------
# B-31（2026-09-17）：副表解析通道（港澳台下辖）。
# 主底座按国标口径不含港澳台下辖（一律无国标码），此前清洗时港澳台地址一律归纳到省级。
# 副表数据存在时（专业版/L1；社区版不带这些数据文件 → 通道自动关闭，退回原省级+0.6 行为），
# 且输入含港澳台指示词或省级命中港澳台时启用：
#   台湾省 → 县市(county) → 乡镇/区(town，注意台湾「区」是乡级)
#   香港   → 18 地方行政区(district)；村名（地址要素）用于推断所属区
#   澳门   → 堂区(parish)；街道名（地址要素）用于推断所属堂区
# 结果路径只含行政区划层（D8 口径：村/街道是地址要素，不作层级），地址要素写入 note 与 codes。
# ---------------------------------------------------------------------------
_SUB_DIR = Path(__file__).parent / "data"
_SUB = {"TW": {}, "HK": {}, "MO": {}}      # 名 → [实体]（行政区划层）
_SUB_ADDR = {"HK": {}, "MO": {}}           # 名 → [实体]（地址要素层：村/街道）
_SUB_UID = {}                              # uid → 实体（取规范名）
_SUB_LOADED = False
_PROV_SUB = {"台湾省": ("TW", "710000000000"),
             "香港特别行政区": ("HK", "810000000000"),
             "澳门特别行政区": ("MO", "820000000000")}
# 指示词（含常用短形式；不用单字"台/港/澳"以免误伤 台州/港北/澳头）
_SUB_IND = (("台湾省", ("台湾省", "台湾"), "TW", "710000000000"),
            ("香港特别行政区", ("香港特别行政区", "香港"), "HK", "810000000000"),
            ("澳门特别行政区", ("澳门特别行政区", "澳门"), "MO", "820000000000"))


def _load_sub_tables():
    global _SUB_LOADED
    if _SUB_LOADED:
        return
    _SUB_LOADED = True

    def _rd(fn):
        p = _SUB_DIR / fn
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    tw = _rd("tw_divisions_v1.json")
    if tw:
        for e in tw.get("entities") or []:
            if e.get("name_zh"):
                _SUB["TW"].setdefault(e["name_zh"], []).append(e)
                _SUB_UID[e["uid"]] = e
    hk = _rd("hkmo_divisions_v1.json")
    if hk:
        for e in hk.get("entities") or []:
            reg = "HK" if str(e.get("uid", "")).startswith("HK-") else "MO"
            _SUB_UID[e["uid"]] = e
            for k in {e.get("name_zh"), e.get("name_raw")}:
                if k:
                    _SUB[reg].setdefault(k, []).append(e)
    hv = _rd("hk_villages_v1.json")
    if hv:
        for e in hv.get("entities") or []:
            for k in {e.get("name"), e.get("name_tc")} | set(e.get("alias") or []):
                if k:
                    _SUB_ADDR["HK"].setdefault(k, []).append(e)
    ms = _rd("mo_streets_v2.json")
    if ms:
        for e in ms.get("entities") or []:
            for k in {e.get("name"), e.get("name_sc"), e.get("name_tc")} | set(e.get("alias") or []):
                if k:
                    _SUB_ADDR["MO"].setdefault(k, []).append(e)


def _canon(uid):
    """uid → 副表规范名（港：元朗区 / 澳：大堂区）。"""
    e = _SUB_UID.get(uid) or {}
    return e.get("name_zh") or e.get("name")


def _sub_match(region, text, pcode, allow_plain=False):
    """副表内解析：返回 resolve/ambiguous 结果 dict，或 None（无命中）。"""
    _load_sub_tables()
    tbl = _SUB.get(region) or {}
    lv = {}
    for nm, entries in tbl.items():
        if len(nm) < 2 or not _match(text, nm):
            continue
        if allow_plain and nm in CURRENT_NAMES:   # 纯名通道：与大陆现行名冲突者不抢
            continue
        lv.setdefault(entries[0].get("level"), []).append((nm, entries))

    # ---- 行政区划层 ----
    if lv:
        if region == "TW":
            def _pick(lvl):
                """整名优先（"新竹市" 不得被 rstrip 短形式"新竹"截去命中"新竹县"）。"""
                lst = lv.get(lvl) or []
                if not lst:
                    return None
                exact = [x for x in lst if x[0] in text]
                return sorted(exact or lst, key=lambda x: -len(x[0]))[0]

            county, town = _pick("county"), _pick("town")
            if county and len({_path_of_tw(e) for e in county[1]}) > 1:
                return _sub_ambiguous(county[1], "台湾省")
            if town and len({(e.get("parent_name"), e.get("name_zh")) for e in town[1]}) > 1:
                return _sub_ambiguous(town[1], "台湾省")
            if town and county and town[1][0].get("parent_name") != county[0]:
                town = None          # 乡镇不属该县市：防"台北市+三民区"混搭
            if town:
                pname = town[1][0].get("parent_name")
                path = (["台湾省", pname, town[0]] if pname else ["台湾省", town[0]])
                return _sub_result(path, pcode, town[1][0]["uid"],
                                   "台湾「区」为乡级（与大陆县级「区」层级不同）")
            if county:
                return _sub_result(["台湾省", county[0]], pcode, county[1][0]["uid"], None)
        else:  # HK / MO
            level = "district" if region == "HK" else "parish"
            _hs = lv.get(level) or []
            _ex = [x for x in _hs if x[0] in text]
            hits = sorted(_ex or _hs, key=lambda x: -len(x[0]))
            if hits:
                nm, entries = hits[0]
                if len({e.get("uid") for e in entries}) > 1:
                    return _sub_ambiguous(entries, "香港特别行政区" if region == "HK" else "澳门特别行政区")
                prov = "香港特别行政区" if region == "HK" else "澳门特别行政区"
                note = "澳门堂区无法人地位，属区划类比层" if region == "MO" else None
                # 附带地址要素（村/街道）信息：不占行政层级（D8 口径），只入 note/codes
                _a = _sub_addr_hit(region, text)
                _auid = None
                if _a:
                    _txt, _auid, _ae = _a
                    note = (note + "；" if note else "") + f"地址要素：{_txt}"
                return _sub_result([prov, entries[0]["name_zh"]], pcode, entries[0]["uid"], note,
                                   addr_uid=_auid)

    # ---- 地址要素层（村/街道）→ 推断所属区/堂区 ----
    _a = _sub_addr_hit(region, text)
    if _a:
        _txt, _auid, e = _a
        if region == "HK":
            duid = e.get("district_uid")
            dname = _canon(duid) or e.get("district_tc")
            if not dname:
                return None
            note = f"地址要素：{_txt}；官方声明仅限选举用途"
            return _sub_result(["香港特别行政区", dname], pcode, duid, note, addr_uid=_auid, conf=0.8)
        puid = e.get("parish_uid")
        pname = _canon(puid) or e.get("parish")
        if not pname:
            return None
        note = f"地址要素：{_txt}"
        return _sub_result(["澳门特别行政区", pname], pcode, puid, note, addr_uid=_auid, conf=0.8)
    return None


def _path_of_tw(e):
    return (e.get("parent_name") or "") + "-" + (e.get("name_zh") or "")


def _sub_addr_hit(region, text):
    """地址要素命中（村/街道）→ (要素名, uid) 或 None。"""
    addr = _SUB_ADDR.get(region) or {}
    hits = sorted(((len(k), k, v) for k, v in addr.items() if len(k) >= 2 and _match(text, k)),
                  key=lambda x: -x[0])
    if not hits:
        return None
    _, key, entries = hits[0]
    e = entries[0]
    label = e.get("name_tc") or e.get("name") or key
    if e.get("rcom_tc"):
        label += f"（{e['rcom_tc']}乡事委员会）"
    elif e.get("parish"):
        label += f"（{e['parish']}，澳门街道非建制）"
    return label, e.get("uid"), e


def _sub_result(path, pcode, uid, note, addr_uid=None, conf=0.9):
    # sub_name/sub_level 供清洗输出的列映射使用（副层级为类比层级：cn_level）
    codes = {"province": pcode}
    e = _SUB_UID.get(uid) or {}
    if e.get("name_zh"):
        codes["sub_name"] = e["name_zh"]
        codes["sub_level"] = e.get("cn_level")
    if uid:
        codes["sub_uid"] = uid
    if addr_uid:
        codes["addr_uid"] = addr_uid
    base = f"副表（{path[0]}下辖无国标码）：" + "-".join(path)
    return {"status": "resolve", "result": "-".join(path), "codes": codes,
            "confidence": conf,
            "note": base + ("；" + note if note else "")}


def _sub_ambiguous(entries, prov):
    cands = []
    for e in entries:
        parent = e.get("parent_name")
        if e.get("level") == "county":
            cands.append(f"{prov}-{e.get('name_zh')}")
        elif parent:
            cands.append(f"{prov}-{parent}-{e.get('name_zh')}")
        else:
            cands.append(f"{prov}-{e.get('name_zh')}")
    return {"status": "ambiguous", "result": list(dict.fromkeys(cands)), "codes": None,
            "confidence": 0.8, "note": f"同名区划多处存在（{prov}副表），输出候选集"}


def _sub_channel(raw, out):
    """在 _resolve_core 结果之上尝试副表解析（B-31）。返回替换结果或 None。"""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    _load_sub_tables()
    if not any(_SUB.values()):
        return None
    # 主解析已给出的省级（首段）——用于「不得越权覆盖大陆解析」的判断
    _main_prov = str(out.get("result") or "").split("-")[0] if isinstance(out.get("result"), str) else ""
    # 1) 输入含港澳台指示词（含短形式）→ 用全区划名匹配（副表名可直接命中）
    for pn, inds, region, pcode in _SUB_IND:
        ind = next((x for x in inds if x in text), None)
        if ind is None:
            continue
        # 守卫①：指示词须在地址头部（位置 0 或前 2 字内），或后接行政区划后缀
        _i = text.find(ind)
        _after = text[_i + len(ind):_i + len(ind) + 1]
        if not (_i == 0 or (_i <= 2 and _after in "区市县岛") or _after in "区市县岛"):
            continue
        # 守卫②：主解析已归属大陆省份的，一律不覆盖（"青岛市市南区香港中路76号" 里的
        # 「香港」是路名，2026-09-17 实测误判为香港南区——回归 B-31）
        if _main_prov and _main_prov not in ("台湾省", "香港特别行政区", "澳门特别行政区") \
                and out.get("status") != "unresolvable":
            return None
        r = _sub_match(region, text, pcode)
        if r:
            if r["result"] == pn:      # 只命中省级本身，不覆盖原省级结果
                return None
            return r
        return None
    # 2) 纯名通道：完全未解析，且名不在大陆现行名集合（避免"松山区"抢内蒙古）
    if out.get("status") == "unresolvable":
        found, single = [], None
        for region, pcode in (("TW", "710000000000"), ("HK", "810000000000"), ("MO", "820000000000")):
            r = _sub_match(region, text, pcode, allow_plain=True)
            if not r:
                continue
            if r["status"] == "ambiguous":
                found.extend(r["result"])
            else:
                found.append(r["result"])
                single = single or r
        found = list(dict.fromkeys(found))
        if len(found) > 1:      # 跨区同名（「中西区」= 香港 ∥ 台南）→ 候选集
            return {"status": "ambiguous", "result": found, "codes": None, "confidence": 0.8,
                    "note": "该名在港澳台副表中多处存在（无国标码），输出候选集"}
        if len(found) == 1:
            return single
    return None

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
    # 音节之间用 "-" 分隔：直接拼接会跨音节碰撞——"西南"=xi+nan 与 "新安"=xin+an 拼接后
    # 同为 "xinan"，会把方位泛称错纠成县名（v1 校准集回归教训）。分隔后同音字仍匹配（"深镇"↔"深圳"）。
    return "-".join(lazy_pinyin(s)).lower()


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

# 道路通名：真实地址里"以省/市短名命名的道路"极多（青岛香港中路、广州北京路、上海南京东路，
# 以及各城市常见的山西路 / 河北路 / 江苏路）。省名**短形式**后面紧接道路通名时，它是路名的一部分、
# 不是区划——不拦就会把「上海市香港路」解析成香港特别行政区（2026-09-16 回归教训）。
_ROAD_TAIL = "路街道巷弄"              # 单字通名：窗口末字命中即判为道路
_ROAD_WORD = ("胡同", "大街", "大道", "环路", "公路")


def _next_looks_like_road(text: str, end: int, span: int = 3) -> bool:
    """从 end 起的 1..span 字窗口是否落在道路通名上（"路" / "中路" / "大街" / "大道"）。"""
    for L in range(1, span + 1):
        seg = text[end:end + L]
        if len(seg) < L:
            break
        if seg in _ROAD_WORD or seg[-1] in _ROAD_TAIL:
            return True
    return False


def _at_boundary(text: str, i: int) -> bool:
    """位置 i 是否处于建制边界：串首 / 前邻非汉字 / 前邻是边界字 / 前缀以**省/市/县名**结尾。
    「宁波鄞州」的鄞州（前缀'宁波'是已知市）算边界，「胶南县」的南县（前缀'胶'）不算。
    只用 UPPER_NAMES（不含乡镇名）做前缀判断，避免短镇名跨词巧合（v3 交付演练回归教训）。"""
    if i == 0:
        return True
    prev = text[i - 1]
    # 前邻非**汉字**即视为边界。注意全角标点（（）、·）码位在汉字区之外、但大于 U+4E00，
    # 不能用简单的"< U+4E00"判断，否则"（山东省）莒县"这类输入里的两字县名会被判成非边界
    # （v3 校准集回归教训）。
    if not ("\u4e00" <= prev <= "\u9fff") or prev in "省市区县盟旗州":
        return True
    return any(text[:i][-_l:] in UPPER_NAMES for _l in (4, 3, 2))


def _apply_alias(text: str) -> str:
    # 防误伤三条：①别名后紧跟建制后缀且拼成已知区划名（如「沙县」+「区」->「沙县区」）时不替换；
    # ②别名必须位于建制边界——单字别名「京」不得命中「南京」词中（v0.3 回归教训）；
    # ③前邻是已知区划名结尾时视为边界（「宁波鄞州」的鄞州）
    for k in sorted(ALIAS_ALL, key=len, reverse=True):
        # 单字别名（京/沪）仅整串生效：否则"京津冀""沪深"会被改造成省级（v2 校准集回归教训）。
        if len(k) == 1 and text != k:
            continue
        # 现行名优先于历史别名（**仅当上下文已定位到现行区划时**）：别名键本身是现行区划名、且其前文
        # 已出现省/市名 → 这里指的是现行那个同名区划，不做历史替换
        # （"安徽省合肥市巢湖市中垾镇"里的"巢湖市"＝现行县级市；而"巢湖市庐江县"里的"巢湖市"＝已撤销
        #   地级市，须替换成"安徽省合肥市" → resolve 到合肥市庐江县。T012 回归教训）。
        if k in KNOWN_NAMES:
            _i0 = text.find(k)
            if text == k:
                continue          # 整串就是现行区划名 → 现行优先（"巢湖市"→现行县级市）
            if _i0 > 0:
                _pre = text[:_i0]
                if any(x in _pre for x in PROV_ANY) or any(x in _pre for x in CITY_ANY):
                    continue
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
            # 别名键是**更长已知名**的前缀时不替换：否则"广西"（→广西壮族自治区）会在
            # "广西壮族自治区"里再替换一次，把全称拼成"广西壮族自治区壮族自治区"（v3 交付演练回归教训）。
            if any(text[i:i + _L] in KNOWN_NAMES and text[i:i + _L] != k
                   for _L in range(len(k) + 1, len(k) + 8)):
                start = i + 1
                continue
            # 别名后面紧接道路通名时不替换：它是路名的一部分，不是区划简称
            # （"上海市香港路"里的"香港"不得展开成"香港特别行政区"，否则整条地址会被
            #  判成香港；"台湾街""江苏路"同理——2026-09-16 回归教训）。
            if _next_looks_like_road(text, j):
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
                # 去后缀短形式不得被"更长且不同的已知区划名"抢走（v0.5/v1 回归教训）：
                # "邵阳市东区"里的"邵阳"是市、"朝阳区"里的"朝阳"是区，都不是"朝阳县"的短形式。
                # 判据：短形式+下一字恰好构成另一个已知区划名（与原 name 不同）→ 该短形式不作数。
                # 后缀限 市/区/县/旗/盟：不含"省"——否则时间机器回放"海南省"（1987）时
                # 会误挡当年的县级"海南区"（T107 回归教训）。
                _nxt = text[i + len(form):i + len(form) + 1]
                if (form != name and _nxt in "市区县旗盟"
                        and (form + _nxt) in KNOWN_NAMES and (form + _nxt) != name):
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
                # 命中窗口本身落在道路通名上（"山西路"距"山西省"仅 1 字）→ 是路名，不是错字
                if _next_looks_like_road(text, i + n):
                    continue
                return form, win
    return None


def _fmt(prov, city, county):
    parts = [prov] + ([city] if city else []) + ([county] if county else [])
    return "-".join(parts)


_MULTI_SEP_PUNCT = "、，,；;＋+/／"      # 标点类分隔符：见到即切
_MULTI_SEP_CONJ = "和与及"               # 连接词类：仅当**不在已知地名内部**时才切


def _inside_known_name(text: str, i: int, maxlen: int = 7) -> bool:
    """位置 i 的字符是否落在某个已知区划名内部（用于判断连接词到底是分隔符还是名字的一部分）。
    "兴和县"的"和"、"呼和浩特市"的"和"都落在名字里，不能当分隔符（v3 交付演练回归教训）。"""
    for _L in range(3, maxlen + 1):
        for _st in range(max(0, i - _L + 1), i + 1):
            seg = text[_st:_st + _L]
            if len(seg) == _L and seg in KNOWN_NAMES:
                return True
    return False


def _multi_split(text: str):
    """合称/多实体拆分：把「北京市和上海市」「杭州、宁波」切成段分别解析。

    两道闸：①连接词（和/与/及）若落在已知地名内部则不视为分隔符；②仅当**每一段都能独立解析**
    且各段结果不全同时才判为 multi——「北京市朝阳区和平里街道」这类含"和"的真实地址因此不受影响。
    段内已无分隔符，故不会递归。
    """
    if not text:
        return None
    segs, cur = [], ""
    for i, ch in enumerate(text):
        if ch in _MULTI_SEP_PUNCT or (ch in _MULTI_SEP_CONJ and not _inside_known_name(text, i)):
            segs.append(cur)
            cur = ""
        else:
            cur += ch
    segs.append(cur)
    segs = [s for s in segs if len(s) >= 2]
    if len(segs) < 2:
        return None
    results = []
    for s in segs:
        o = _resolve_core(s)
        if o["status"] not in ("resolve", "special"):
            return None
        results.append(o["result"])
    if len({str(r) for r in results}) < 2:
        return None
    return {"status": "multi", "result": results, "codes": None, "confidence": 0.9,
            "note": "合称/多实体：按分隔符拆分为多个区划实体"}


def _resolve_core(raw: str) -> dict:
    """核心解析：纯函数，无状态。返回 {status, result, codes, confidence, note}。"""
    raw_text = _strip_noise(raw)
    text = _apply_alias(raw_text)

    for w in UNRESOLVABLE_WORDS:
        if w in text:
            return {"status": "unresolvable", "result": None, "codes": None, "confidence": 0.0,
                    "note": f"区域泛称/占位符「{w}」不解析，诚实拒绝"}

    if text in REGION_GENERIC:
        return {"status": "unresolvable", "result": None, "codes": None, "confidence": 0.0,
                "note": f"区域泛称「{text}」非行政区划，诚实拒绝"}

    # 多实体拆分用**别名展开前**的原文：别名会把"呼和浩特郊区"展开成"内蒙古自治区-呼和浩特市-赛罕区"，
    # 其中的"和"字会被分隔符误切（v1 校准集回归教训）。
    _m = _multi_split(raw_text)
    if _m:
        return _m

    for key, (loc, why) in SPECIAL_DIRECT.items():
        if key in text:
            _loc = loc
            # 「需按上级城市落位」类（如"高新技术产业开发区"）：所属地级可从原文
            # 开头的地级短名确定（B-23，2026-09-17）
            if "需按上级城市" in loc:
                _dev = _dev_city_at_head(text)
                if _dev:
                    _loc = _dev
            return {"status": "special", "result": _loc, "codes": None, "confidence": 0.8,
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
            if m != p:
                # 短形式命中：若该处其实是**更长已知区划名**的前缀（"海南藏族自治州"里的"海南"、
                # "海南区"里的"海南"），不算省命中——否则自治州/区会被误判成省（v2 校准集回归教训）。
                _i = text.find(m)
                if any(text[_i:_i + _L] in KNOWN_NAMES and text[_i:_i + _L] != p
                       for _L in range(len(m) + 1, len(m) + 10)):
                    continue
                # 短形式后面紧接道路通名 → 它是路名的一部分，不是区划
                # （"上海市香港路"不得落到香港特别行政区；"山西路"不得落到山西省）。
                # 仅对**短形式**生效：全名（"山西省太原市"）不受影响。
                if _next_looks_like_road(text, _i + len(m)):
                    continue
            prov_found.append((p, 1.0 if m == p else 0.9, m))
        else:
            f = _fuzzy_in(text, p)
            if f:
                form, win = f
                # 模糊匹配（字距 1）须过两道闸，否则极易误伤：
                # ①命中窗口本身已是现行区划名 → 不是错字，是另一个词（"南京市"≠"北京市"）；
                # ②拼音不验证 → 读音都不同，多半是别的词（"南京市"nan-jing-shi ≠"北京市"bei-jing-shi）。
                if win in KNOWN_NAMES or win in KNOWN_SHORT:
                    continue
                if _HAS_PY and _pinyin_key(win) != _pinyin_key(form):
                    continue
                prov_found.append((p, 0.8, form))

    if not prov_found:
        # 地级全名优先：输入**本身就是**地级名（市/地区/自治州/盟）时，应落到该地级，而不是它下辖的
        # 同名县市——"喀什地区"≠"喀什市"、"和田地区"≠和田市/和田县、"红河哈尼族彝族自治州"≠红河县
        # （v3 探针回归教训）。仅整串相等时生效，避免抢走"杭州市西湖区"这类含下级的输入。
        _full_city = [(p, c) for p, pv in DIV.items() for c in pv["cities"] if c == text]
        if len(_full_city) == 1:
            p, c = _full_city[0]
            return {"status": "resolve", "result": _fmt(p, c, None),
                    "codes": {"province": DIV[p]["code"],
                              "city": DIV[p]["cities"][c].get("code"), "county": None},
                    "confidence": 1.0, "note": "地级（市/地区/自治州/盟）全名直配"}
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
        # 文本显式含地级市名、但县级候选无一属于该市时，不得跨省张冠李戴（v0.5 回归教训：
        # "邵阳市东区"曾落到四川攀枝花东区）。改为落到该市并如实标注区县级未识别。
        if not refined:
            def _city_seen_ok(_c):
                """文本里是否真的出现了这个地级名（**边界感知**），防"峨眉山市"被当成"眉山市"。"""
                _r = _match_at(text, _c, boundary2=True)
                if not _r:
                    return False
                _f, _pp = _r
                return not (_pp > 0 and text[_pp - 1:_pp + len(_f)] in KNOWN_NAMES)

            _c_seen = max((c for p, pv in DIV.items() for c in pv["cities"] if c and _city_seen_ok(c)),
                          key=len, default=None)
            if _c_seen:
                for p, pv in DIV.items():
                    if _c_seen in pv["cities"]:
                        return {"status": "resolve", "result": _fmt(p, _c_seen, None),
                                "codes": {"province": pv["code"],
                                          "city": pv["cities"][_c_seen].get("code"), "county": None},
                                "confidence": 0.7,
                                "note": f"市级命中；「{text}」的区县级在该市现行区划中无匹配（可能已撤销或更名）"}
        hits = refined or hits
        fulls = [h for h in hits if h.get("full")]
        if len(hits) > 1 and fulls and len(fulls) < len(hits):
            hits = fulls  # 全名命中优先于短形式（鄂托克前旗 vs 鄂托克旗——v0.6 回归教训）
        if len(hits) > 1 and len({h["pos"] for h in hits}) > 1:
            # 多县命中但位置不同：地址语序靠前者胜出（"昆山开发区前进东路"——昆山在句首，前进区在佳木斯）
            hits = [min(hits, key=lambda h: h["pos"])]
        _ev_town = None
        if len(hits) > 1:
            # 街道证据消歧（B-24，2026-09-17）：同名县多处时，若文本中的乡镇/街道名
            # （≥3 字）唯一归属某一候选县，以其消歧；证据冲突/无证据维持歧义。
            _ev = _township_evidence(hits, text)
            if _ev:
                hits, _ev_town = [_ev[0]], _ev[1]
        if len(hits) == 1:
            h = hits[0]
            # B-30（2026-09-17）：直配命中的县名若同时也是某撤销迁移的旧名，
            # 裸名输入不得静默选边——按前缀定向或输出双候选集。
            _migs = [m for m in (_ABOLISHED_BY_NAME.get(h["path"][-1]) or [])
                     if "-".join(m["path"]) != "-".join(h["path"])]
            if _migs and not _ev_town:
                _cur_prov = h["path"][0]
                _pick_mig = [m for m in _migs if m["prov"] in text]
                if _pick_mig:
                    _m = _pick_mig[0]
                    _cpath, _ccodes = CODE_PATH.get(_m["code"], ([], {}))
                    return {"status": "resolve", "result": "-".join(_cpath), "codes": dict(_ccodes),
                            "confidence": 0.8,
                            "note": f"「{h['path'][-1]}」已于 {_m['year']} 年撤销，现属{_m['name']}，按现行区划解析"}
                # 本级路径前缀（省/市）出现在输入里 → 定向现行，不再出双候选
                # （「宁波市江北区解放路63号」含市名即定向；此前仅看省名导致整条变候选集——演练回归）
                _cur_tokens = [seg for seg in h["path"][:-1]
                               if len(seg) >= 2 and seg in text]
                if not _cur_tokens and _cur_prov not in text:
                    _cands = ["-".join(h["path"])] + ["-".join(m["path"]) for m in _migs]
                    return {"status": "ambiguous", "result": _cands, "codes": None,
                            "confidence": 0.8,
                            "note": ("同名区划并存：「" + h["path"][-1] + "」既有现行建制，"
                                     "也是已撤销迁移的旧名，输出候选集")}
            result_str, hcodes = "-".join(h["path"]), dict(h["codes"])
            ccode = hcodes.get("county")
            for tn in COUNTY_TOWNSHIPS.get(ccode or "", {}):
                if _match(text, tn):
                    result_str += "-" + tn
                    hcodes["township"] = COUNTY_TOWNSHIPS[ccode][tn]
                    break
            return {"status": "resolve", "result": result_str, "codes": hcodes,
                    "confidence": 0.8 if _ev_town else 1.0,
                    "note": ("同名县多处，以街道证据「" + _ev_town + "」消歧"
                             if _ev_town else "区县直配反推上级")}
        return {"status": "ambiguous", "result": ["-".join(h["path"]) for h in hits], "codes": None,
                "confidence": 0.9, "note": "同名区县多处存在，输出候选集"}

    best_p, conf, best_m = max(prov_found, key=lambda x: (x[1], len(x[2])))
    rest = text.replace(best_m, "", 1)
    entry = DIV[best_p]
    entries = _entries(entry["cities"])

    hit_city, city_code, county_pool = None, None, []
    _city_hits = []
    for c, ccode, counties in entries:
        if not c:
            continue
        r = _match_at(rest, c, boundary2=True)
        if not r:
            continue
        _form, _pos = r
        # 匹配窗口前一字符若能扩展成更长的已知名，则这个"市"实际是更长名的一部分（"峨眉山市"里的"眉山市"）
        if _pos > 0 and rest[_pos - 1:_pos + len(_form)] in KNOWN_NAMES:
            continue
        # 市名短形式可能只是本市区县名的前缀（"黄石港区"含"黄石"）：若同位置有更长的本市区县名，交给县级解析
        if any(cn.startswith(_form) and cn != _form and _match(rest, cn) for cn in counties):
            continue
        # 全名命中优先；否则按出现位置取最早（地址语序）——"…榕城区中山路"里的"中山"不该压过句首的揭阳市
        _city_hits.append((0 if _form == c else 1, _pos, -len(_form), c, ccode, counties, _form))
    if _city_hits:
        _city_hits.sort(key=lambda x: x[:3])
        _f, _p, _ln, hit_city, city_code, county_pool, _form = _city_hits[0]
        rest = rest.replace(_form, "", 1)
    if hit_city is None:
        county_pool = [cn for _, _, counties in entries for cn in counties]

    # 两字县名须边界匹配：否则"西藏自治区白朗县"会把"朗县"当第二个候选（v1 校准集回归教训）。
    county_hits = [cn for cn in county_pool if _match(rest, cn, boundary2=True)]
    if len(county_hits) > 1:
        fulls = [cn for cn in county_hits if _match_at(rest, cn)[0] == cn]
        if fulls and len(fulls) < len(county_hits):
            county_hits = fulls  # 全名命中优先（鄂托克前旗 vs 鄂托克旗——v0.6 回归教训）
    codes = {"province": entry["code"], "city": city_code, "county": None}

    def _county_owners(cn):
        """省内的县级名 -> [(地级名, 地级码, 县码)]；只统计**有地级**的桶
        （直辖市/省直辖县级挂在 "" 键下，交给下方原逻辑处理）。"""
        return [(cname, cvan.get("code"), cvan["counties"][cn])
                for cname, cvan in DIV[best_p]["cities"].items()
                if cname and cn in (cvan.get("counties") or {})]

    if len(county_hits) == 1 and hit_city is None:
        # 省内县级命中但文本未含地级名：必须**反查真实所属地级**，否则会丢市级并让县级码为 null
        # （"浙江省西湖区"曾返回"浙江省-西湖区"、county 码 null —— v3 探针回归教训）。
        _owners = _county_owners(county_hits[0])
        if len(_owners) > 1:
            # 同名县在本省多处（如河北省三个桥西区）：输出候选（且不得出现重复项）
            return {"status": "ambiguous",
                    "result": sorted(_fmt(best_p, cname, county_hits[0]) for cname, _, _ in _owners),
                    "codes": None, "confidence": 0.9,
                    "note": "同名区县在本省多处存在，输出候选集"}
        if len(_owners) == 1:
            _cn = county_hits[0]
            hit_city, city_code, _ccode = _owners[0]
            codes["city"], codes["county"] = city_code, _ccode
            _rest = rest.replace(_cn, "", 1)
            for tn in COUNTY_TOWNSHIPS.get(_ccode or "", {}):
                if _match(_rest, tn):
                    codes["township"] = COUNTY_TOWNSHIPS[_ccode][tn]
                    return {"status": "resolve", "result": _fmt(best_p, hit_city, _cn) + "-" + tn,
                            "codes": codes, "confidence": max(conf, 0.9),
                            "note": "省内县级+乡镇解析（补全所属地级）"}
            return {"status": "resolve", "result": _fmt(best_p, hit_city, _cn), "codes": codes,
                    "confidence": conf, "note": "省内县级直配（补全所属地级）"}

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
        # 候选须去重并按（地级, 县）展开：省内同名县多处时，旧实现会产出重复项且丢市级
        # （"河北省桥西区"曾返回 ['河北省-桥西区','河北省-桥西区'] —— v3 探针回归教训）。
        _scope = ({hit_city: DIV[best_p]["cities"].get(hit_city)} if hit_city
                  else DIV[best_p]["cities"])
        cands = sorted({_fmt(best_p, cname, cn)
                        for cn in set(county_hits)
                        for cname, cvan in _scope.items()
                        if cvan and cn in (cvan.get("counties") or {})})
        if len(cands) == 1:
            return {"status": "resolve", "result": cands[0], "codes": codes,
                    "confidence": conf, "note": "同级候选去重后唯一（逐级匹配）"}
        return {"status": "ambiguous", "result": cands,
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
        # 开发区/园区非正式建制（不虚构码），但所属地级市可确定（B-23，2026-09-17）：
        # rest 以省内地级市去"市"短名开头时，补全所属地级。
        _dev_city = None
        if not hit_city:
            for c, _cc, _cns in entries:
                if c and c.endswith("市") and len(c) >= 3 and rest.startswith(c[:-1]):
                    _dev_city = c
                    break
        return {"status": "special", "result": _fmt(best_p, hit_city or _dev_city, None), "codes": codes,
                "confidence": 0.8, "note": "非正式区划（开发区/园区等），落到上级并标注口径"}
    if hit_city:
        # 市级命中但残留文本仍声称下级建制：不静默吞掉，沿用 0.7 档（下级无匹配）并明说
        seg = _residual_admin_seg(rest)
        if seg:
            return {"status": "resolve", "result": _fmt(best_p, hit_city, None), "codes": codes,
                    "confidence": 0.7,
                    "note": f"解析到地级市；「{seg}」未能在该市下级区划中核实（可能已撤销或更名）"}
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
    # 省级命中但残留文本仍声称下级建制：不得按满档置信输出（0.6 档——
    # 「省级确定、残留下级未核实」），港澳台下辖另注明无国标码、副表只读可查
    if conf >= 0.8:
        seg = _residual_admin_seg(rest)
        if seg:
            _mig = (_ABOLISHED.get(best_p) or {}).get(seg)
            if _mig:
                _nc2 = _mig["code"]
                _cpath, _ccodes = CODE_PATH.get(_nc2, ([], {}))
                return {"status": "resolve", "result": "-".join(_cpath), "codes": dict(_ccodes),
                        "confidence": 0.8,
                        "note": f"「{seg}」已于 {_mig['year']} 年撤销，现属{_mig['name']}，按现行区划解析"}
            _tw = best_p in ("台湾省", "香港特别行政区", "澳门特别行政区")
            _sub = "；该下辖无国标码，不在解析范围（独立副表只读可查）" if _tw else ""
            return {"status": "resolve", "result": _fmt(best_p, None, None), "codes": codes,
                    "confidence": 0.6,
                    "note": f"解析到省级；「{seg}」未能核实为下级区划{_sub}"}
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
                                           "county": cv["counties"][cn]}, "pos": r[1],
                                 "full": r[0] == cn})
    return hits or None


def _depth(out) -> int:
    """已解析出的层级数（省/市/区县/乡镇里有码的个数）。"""
    c = out.get("codes") or {}
    return sum(1 for k in ("province", "city", "county", "township") if c.get(k))


def _shallow(out) -> bool:
    """结果是否"浅"到值得再试一次拼音纠错：没解析出来、歧义、或只落到省级/special 无区县。"""
    st = out.get("status")
    if st in ("unresolvable", "ambiguous"):
        return True
    if st in ("resolve", "special"):
        return _depth(out) <= 1
    return False


def _text_is_road(t: str) -> bool:
    """整串是否就是一条道路名（末字/末词为道路通名）。用于阻断"路名被当区划"的纠错补救。
    「山西路」若主解析已被道路守卫挡下，拼音纠错会把它救成"陕西省"（山西/陕西 无调拼音同为
    shanxi）——那还不如老实说不知道（2026-09-16 回归教训）。"""
    if not t:
        return False
    return t[-1] in _ROAD_TAIL or any(t.endswith(w) for w in _ROAD_WORD)


# 行政区划通名（长通名在前，防"区"抢在"堂区/街道"之前命中）。
_ADMIN_TAILS = ("街道", "堂区", "林区", "矿区", "新区", "特区",
                "区", "市", "县", "旗", "盟", "镇", "乡", "村")


def _residual_admin_seg(rest: str):
    """上级解析后，残留文本中是否仍有以建制通名收尾的片段；有则返回该片段（供 note 引用）。

    动机：省/市级命中后，残留的"下级声称"曾被静默吞掉并按满档置信输出——
    「香港中西区」「台湾台北市」「广东省龙泉驿区」（龙泉驿区在成都，不在广东）
    都返回 conf=1.0 的"解析到省级"，等于把"我没数据"包装成"我确定"（2026-09-16 回归教训）。
    通名前至少 1 字专名才成段（纯「区」「市」不算）。"""
    t = _strip_noise(rest or "")
    if len(t) < 2:
        return None
    for tail in _ADMIN_TAILS:
        j = t.rfind(tail)
        if j >= 1:
            return t[max(0, j - 6):j + len(tail)]
    return None


def _township_evidence(hits, text):
    """B-24 街道证据消歧：同名县多候选时，找文本中归属唯一的乡镇/街道名。
    返回 (hit, 街道名) 或 None。街道名须 ≥3 字防两字巧合；两个以上候选均有
    归属街道时返回 None（证据冲突，维持歧义）。"""
    matched = None
    for h in hits:
        ccode = (h.get("codes") or {}).get("county")
        for tn in COUNTY_TOWNSHIPS.get(ccode or "", {}):
            if len(tn) >= 3 and _match(text, tn):
                if matched and matched[0] is not h:
                    return None
                if not matched:
                    matched = (h, tn)
                break
    return matched


def _dev_city_at_head(text: str):
    """text 以某地级市去"市"短名开头时返回该地级全名（开发区/园区落所属地级，B-23）。"""
    if not text:
        return None
    for p, pv in DIV.items():
        for c, cv in pv["cities"].items():
            if c and c.endswith("市") and len(c) >= 3 and text.startswith(c[:-1]):
                return _fmt(p, c, None)
    return None


def _hard_reject(raw: str) -> bool:
    """输入是否为"刻意拒绝"类（区域泛称/占位符/纯道路名）。刻意拒绝不得再走拼音纠错——
    否则「华南」(huá nán) 会被同音纠成「桦南县」、「西南」被纠成「新安县」（v1 校准集回归教训），
    「山西路」会被救成「陕西省」（2026-09-16 回归教训）。"""
    t = _apply_alias(_strip_noise(raw or ""))
    return (t in REGION_GENERIC or any(w in t for w in UNRESOLVABLE_WORDS)
            or _text_is_road(t))


def resolve(raw: str) -> dict:
    """对外入口：核心解析 + （unresolvable/ambiguous 时）一次拼音纠错重试。
    纠错采纳规则：unresolvable 态接受任何有效解析；ambiguous 态仅当纠错后变为唯一解析
    才采纳（"通州区"类真歧义不被破坏，"杭洲市西湖区"类错字歧义被修复）。
    刻意拒绝的输入（区域泛称等）不纠错。"""
    out = _resolve_core(raw)
    # 副表解析通道（B-31，2026-09-17）：港澳台下辖不在主底座（无国标码），
    # 副表数据存在时（专业版/L1）在此升级到区/堂区/县市/乡镇层；社区版无该数据 → 自动关闭。
    _subout = _sub_channel(raw, out)
    if _subout:
        return _subout
    # 纠错门控（B-22/B-23，2026-09-17）：①_hard_reject（泛称/占位符/纯道路名）只在
    # unresolvable 态阻断——ambiguous 态已含区划命中，末字"路"是地址正常组成；
    # ②SPECIAL_DIRECT「特殊口径」结论不进纠错（「园区」曾被乱纠成垣曲县）。
    _blocked = ((_hard_reject(raw) and out["status"] == "unresolvable")
                or "特殊口径" in (out.get("note") or ""))
    if (_HAS_PY and raw and raw.strip() and _shallow(out)
            and not _blocked):
        try:
            hit = _pinyin_retry(raw.strip())
        except Exception:
            hit = None
        if hit:
            out2, seg, name = hit
            if out["status"] == "unresolvable":
                adopt = out2["status"] != "unresolvable"      # 完全没解析出来：任何有效解析可采纳
            elif out["status"] == "ambiguous":
                adopt = out2["status"] == "resolve" and len(seg) >= 3   # 真歧义：仅长段纠出唯一解析才改口
            else:
                adopt = _depth(out2) > _depth(out)            # 浅解析：仅纠错后层级更深才采纳
            if adopt:
                out2["confidence"] = min(out2.get("confidence") or 0.8, 0.8)
                out2["note"] = f"拼音纠错「{seg}」→「{name}」；{out2['note']}"
                return out2
    if raw and raw.strip() and _shallow(out) and not _blocked:
        # 形近字第二道（拼音不同音、部首/字形相近的高频地址错字；B-22，2026-09-17：
        # 「深玔市」玔 chuàn ≠ 圳 zhèn，同音纠错够不着）。采纳条件比拼音更严。
        hit2 = _form_retry(raw.strip())
        if hit2:
            out2, wrong, right = hit2
            adopt = (out2["status"] in ("resolve", "special")
                     and _depth(out2) >= _depth(out))
            if adopt:
                out2["confidence"] = min(out2.get("confidence") or 0.8, 0.8)
                out2["note"] = f"形近纠错「{wrong}」→「{right}」；{out2['note']}"
                return out2
    # 文本证据消歧（B-29 附带）：ambiguous 候选中恰有一个候选末段名是输入子串、
    # 且输入还含其他区划内容时，以文本证据择一（conf 0.8）。裸名歧义维持。
    if (out["status"] == "ambiguous" and isinstance(out.get("result"), list)
            and raw):
        _rs = raw.strip()
        # B-30 约束：输入必须长于命中段本身（裸名琐碎匹配不算证据）
        _hits = [r for r in out["result"]
                 if len(r) > len(_rs) and len(_rs) > len(r.split("-")[-1])
                 and r.split("-")[-1] in _rs]
        if len(_hits) == 1:
            _pick = _hits[0]
            _pc = None
            for _c, _pth in CODE_PATH.items():
                if "-".join(_pth[0]) == _pick:
                    _pc = dict(_pth[1]); break
            return {"status": "resolve", "result": _pick, "codes": _pc,
                    "confidence": 0.8,
                    "note": "同名候选中「" + _pick.split("-")[-1] +
                            "」为输入所指（文本证据），消歧"}
    # 形近候选集（B-27）：白名单未命中时，按算法相关度表输出候选——不猜唯一，
    # 复用 ambiguous 契约（result=候选列表），conf 0.5 = 候选未核实。
    if out["status"] == "unresolvable" and raw and raw.strip() and not _blocked:
        try:
            cands = _form_candidates(raw.strip())
        except Exception:
            cands = None
        if cands:
            _o, _sc, _w, _r = cands[0]
            return {"status": "ambiguous",
                    "result": [o["result"] for o, sc, w, r in cands],
                    "codes": None, "confidence": 0.5,
                    "note": (f"疑似形近错字「{_w}」→「{_r}」（相关度 {_sc}），"
                             "输出候选集供选择；相关度是相似度分，不是准确率")}
    return out


# 形近字对（错字→正字）：只收高频、把握大的地址错字，持续登记；宁缺勿滥。
# 白名单（人工确认）→ 自动纠错为唯一答案；长尾（未确认）→ 走 _form_candidates 候选集。
_FORM_SIMILAR = {"玔": "圳"}

# 形近相关度表（B-27，算法生成）：wrong -> [(right, score)...]。只用于输出候选集，
# 不自动采纳为唯一答案——『写对就一种，写错无数种』（2026-09-17 定案）。
_FORM_TABLE_FILE = Path(__file__).parent / "data" / "form_similar_v1.json"
_FORM_TABLE: dict[str, list] = {}
try:
    _ft = json.loads(_FORM_TABLE_FILE.read_text(encoding="utf-8")).get("table") or {}
    _FORM_TABLE = {k: v for k, v in _ft.items() if isinstance(v, list) and v}
except Exception as _e:
    print(f"[warn] form_similar_v1.json 加载失败，形近候选集不可用: {_e}")


def _form_retry(raw: str):
    """形近字纠错重试：把 raw 中的形近错字替换为正字后重解析。返回 (out2, wrong, right) 或 None。"""
    if not raw:
        return None
    for wrong, right in _FORM_SIMILAR.items():
        if wrong in raw and right not in raw:
            out2 = _resolve_core(raw.replace(wrong, right))
            if out2["status"] in ("resolve", "special", "ambiguous"):
                return out2, wrong, right
    return None


_FORM_CAND_TOPK = 3  # 每个字位只试相关度前 3 的候选（控制试探成本）


def _form_candidates(raw: str):
    """形近候选集（B-27）：主解析失败时，对 raw 逐字查形近相关度表做单字替换试探，
    收集替换后能解析出区划的结果，按相关度降序。**只输出候选、不采纳为唯一答案**。"""
    if not raw or len(raw) > 30 or not _FORM_TABLE:
        return None
    outs: dict[str, tuple] = {}
    for i, ch in enumerate(raw):
        for right, sc in _FORM_TABLE.get(ch, ())[:_FORM_CAND_TOPK]:
            o = _resolve_core(raw[:i] + right + raw[i + 1:])
            if o["status"] == "resolve":
                prev = outs.get(o["result"])
                if prev is None or sc > prev[1]:
                    outs[o["result"]] = (o, sc, ch, right)
    if not outs:
        return None
    return sorted(outs.values(), key=lambda x: -x[1])


def _pinyin_retry(text: str):
    """滑窗取 2-6 字段做全拼匹配（同音不同字），长段优先。

    候选择优：优先返回**唯一解析**（resolve/special）的候选，全部候选都只能得到歧义时才退而返回
    首个歧义结果。否则同音多名并存时会随机落到低层级歧义上（"西鞍"→西安区×2 而非西安市——
    v2 校准集回归教训）。
    """
    if PY_INDEX is None:
        _build_py_index()
    fallback = None
    for n in (6, 5, 4, 3, 2):
        for i in range(0, len(text) - n + 1):
            seg = text[i:i + n]
            # 该段已是"合法写法"而非错字，跳过整段：
            #   ①现行区划名（"西湖区""朝阳区"）——是真同名歧义；
            #   ②别名词典里的已知别名（"新疆"）——否则"新疆维吾尔自治区"里的"新疆"会被纠成新绛县。
            if seg in KNOWN_NAMES or seg in KNOWN_SHORT or seg in ALIAS_ALL:
                continue
            for name, path in PY_INDEX.get(_pinyin_key(seg), []):
                # 输入段已是该实体的去后缀短名（"朝阳"→朝阳市/县/区）不算错字，跳过——防破坏真歧义
                if name == seg or name.rstrip("省市区县盟旗") == seg:
                    continue
                out2 = _resolve_core(text[:i] + name + text[i + n:])
                if out2["status"] in ("resolve", "special"):
                    return out2, seg, name
                if out2["status"] != "unresolvable" and fallback is None:
                    fallback = (out2, seg, name)
    return fallback


def resolve_any(text: str) -> dict:
    """统一入口：纯数字串走编码直查；文中含 6 位地址码时优先做编码解读
    （「510124是哪里」→历史码归属）；否则走地址解析。API 服务端用。"""
    t = text.strip()
    if t and not (set(t) - set("0123456789 -")):
        return resolve_by_code(t)
    import re as _re
    m = _re.search(r"\d{6}", t)
    if m and _re.sub(r"\d", "", t):
        out = resolve_by_code(m.group(0))
        if out["status"] != "unresolvable":
            out["note"] = "识别到地址码「" + m.group(0) + "」；" + str(out.get("note") or "")
            return out
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


def query_events(year=None, q=None, code=None, limit=50, year_start=None, year_end=None):
    """变更事件库查询（L1 版本查询端点核心）：按年份/年份区间/关键词/区划码过滤，纯函数。
    code 支持 6/12 位（旧码新码均可，自动补零）；q 对事件全文字段做子串匹配。"""
    year = int(year) if year else None
    year_start = int(year_start) if year_start else None
    year_end = int(year_end) if year_end else None
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
        if year_start and e["year"] < year_start:
            continue
        if year_end and e["year"] > year_end:
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
            "filter": {"year": year, "year_start": year_start, "year_end": year_end, "q": q, "code": c},
            "note": "变更事件库 1980-2026（官方代码簿差分+策展）；doc_no 增量回填中"}


def dual_code(query: str) -> dict:
    """口径差异披露（社区版行为）：只说明民政 × 统计两套口径的差异点，不返回对照表。

    边界（勿改）：双码交叉表（民政 ↔ 统计逐条对照 + 按县覆盖率）属专业版数据包，
    不随社区版分发。社区版保留本函数是为了「诚实披露差异」这一信誉能力，
    一旦把对照表放进 data/，该付费点即归零。
    """
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
        "省市县乡四级": "统计用区划代码与民政 12 位码同源 GB/T 2260；省/县两级一致性好，"
                    "乡段经 2026-09 全量实证在部分县存在两库编号分歧",
        "村级": "统计村级 12 位码与城乡分类代码自 2024-10 起无现行公开渠道（2023 版为最后一期）。"
               "经全量实证（民政地名库现行 × NBS 2023 冻结基线）：村级可映射率县际差异大"
               "（中位 20%，19% 县 ≥80%），按县实测覆盖率随 L2 数据集交付，不做伪造映射",
        "开发区专项段": "统计口径存在开发区专项代码段，民政口径无此建制——引擎按特殊口径标注",
    }
    out = {"status": base["status"], "result": base.get("result"),
           "mca": mca, "nbs": nbs, "note": base.get("note")}
    if base["status"] == "historical":
        out["historical"] = {"old_code": codes.get("old_code"), "current_code": codes.get("current_code")}
    return out


# ---------------------------------------------------------------------------
# 时间机器（终极形态核心）：resolve_at(raw, year) —— 任意年份的区划状态回放
# 数据：GB/T 2260 官方口径逐年快照（1980-2020，@cndiv/source-history 固化）
# ---------------------------------------------------------------------------
GB_CSV = Path(__file__).parent / "sources" / "gb2260" / "package" / "data" / "divisions.csv"
_GB_YEARS = None   # {year: {code: (name, parent, level)}}


def _gb_load():
    global _GB_YEARS
    if _GB_YEARS is None:
        import csv
        from collections import defaultdict
        cache = defaultdict(dict)
        # 未见快照文件时降级为空表（社区版不随包分发 GB/T 2260 快照）：
        # 由 resolve_at 返回"不可用"的诚实拒绝，而不是抛 FileNotFoundError（v3 回归教训）。
        if not GB_CSV.exists():
            _GB_YEARS = {}
            return _GB_YEARS
        with open(GB_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                y = int(r["year"])
                cache[y][r["code"]] = (r["name"], r["parent_code"], int(r["level"]))
        # 2021 为部分变更快照（21 行），不作为可回放年份
        _GB_YEARS = {y: d for y, d in cache.items() if len(d) > 1000}
    return _GB_YEARS


def _gb_path(rows, code):
    parts, c = [], code
    for _ in range(4):
        if c not in rows:
            break
        name, parent, lvl = rows[c]
        parts.append(name)
        c = parent
        if lvl == 1:
            break
    return list(reversed(parts))


def resolve_at(raw: str, year) -> dict:
    """时间机器：给定名称/地址与年份，回放该年的区划状态。纯函数。
    返回 {status, result, codes:{code, province, city}, now:{...}|None, note}。
    当年无此建制时诚实拒绝，并附现行对照（now）——时间机器的"错位提示"。"""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return {"status": "unresolvable", "result": None, "codes": None, "now": None,
                "note": "year 须为整数年份"}
    gb = _gb_load()
    if not gb:
        return {"status": "unresolvable", "result": None, "codes": None, "now": None,
                "note": "本发行版未随包分发 GB/T 2260 逐年快照，时间机器不可用"}
    if y not in gb:
        return {"status": "unresolvable", "result": None, "codes": None, "now": None,
                "note": f"可回放年份范围 {min(gb)}-{max(gb)}（GB/T 2260 快照覆盖期）"}
    rows = gb[y]
    text = _strip_noise(raw or "")
    if not text:
        return {"status": "unresolvable", "result": None, "codes": None, "now": None,
                "note": "输入为空"}
    # 当年名称索引（省/市/县），带边界规则防词中误伤
    hits = []
    for code, (name, parent, lvl) in rows.items():
        if lvl not in (1, 2, 3):
            continue
        r = _match_at(text, name, boundary2=True)
        if r:
            hits.append({"code": code, "name": name, "lvl": lvl, "pos": r[1],
                         "full": r[0] == name})
    now = None
    if not hits:
        cur = _resolve_core(text)
        if cur["status"] in ("resolve", "special"):
            now = {"result": cur["result"], "codes": cur.get("codes")}
            return {"status": "unresolvable", "result": None, "codes": None, "now": now,
                    "note": f"{y} 年快照中无此建制（当年不存在或已撤销）；现行为「{cur['result']}」"}
        return {"status": "unresolvable", "result": None, "codes": None, "now": None,
                "note": f"{y} 年快照中未识别该区划"}
    # 同名多码 -> 歧义；用上级名在文本中出现与否精化
    by_name = {}
    for h in hits:
        by_name.setdefault(h["name"], []).append(h)
    cands = []
    for name, hs in by_name.items():
        refined = [h for h in hs if any(rows[p][0].rstrip("市") in text
                                        for p in [h["code"]] if False) or True]
        # 精化：上级路径任意一段出现在文本中
        refined = []
        for h in hs:
            path = _gb_path(rows, h["code"])
            if any(seg.rstrip("市") in text for seg in path[:-1]) or len(hs) == 1:
                refined.append(h)
        cands.extend(refined or hs)
    # 全名命中优先于去后缀短形式（"芜湖县"须胜过同位置的短形式"芜湖"→芜湖市；
    # "阿拉善左旗"胜过"阿拉善盟"；"大通回族土族自治县"胜过"大通区"——v3 校准集回归教训）
    fulls = [h for h in cands if h.get("full")]
    if len(cands) > 1 and fulls and len(fulls) < len(cands):
        cands = fulls
    # 位置优先；同位并列时高建制级别优先（"海南省"应胜过乌海市海南区——v0.5 回归教训）
    cands.sort(key=lambda h: (h["pos"], h["lvl"]))
    top_name = cands[0]["name"]
    finals = [h for h in cands if h["name"] == top_name]
    if len(finals) > 1 and len({(h["pos"], -h["lvl"]) for h in finals}) > 1:
        finals = [min(finals, key=lambda h: (h["pos"], h["lvl"]))]
    if len(finals) > 1:
        return {"status": "ambiguous",
                "result": ["-".join(_gb_path(rows, h["code"])) for h in finals],
                "codes": None, "now": None, "note": f"{y} 年同名区划多处存在，输出候选集"}
    h = finals[0]
    path = _gb_path(rows, h["code"])
    codes = {"code": h["code"], "province": path[0]}
    if len(path) > 1:
        codes["city"] = path[1] if rows[h["code"]][2] >= 2 and h["code"][:2] + "0000" != h["code"] else None
    # 现行对照：若该码已不存在，查事件链
    cur_code = h["code"]
    hops = 0
    while cur_code in OLD_CODE_MAP and hops < 6:
        cur_code = OLD_CODE_MAP[cur_code]["code"]
        hops += 1
    if cur_code in CODE_PATH and cur_code != h["code"]:
        now = {"result": "-".join(CODE_PATH[cur_code][0]), "code": cur_code}
    elif h["code"] in CODE_PATH:
        now = {"result": "-".join(CODE_PATH[h["code"]][0]), "code": h["code"]}
    lvl_note = {1: "省级", 2: "地级", 3: "县级"}.get(rows[h["code"]][2], "")
    return {"status": "resolve", "result": "-".join(path), "codes": codes, "now": now,
            "confidence": 1.0, "note": f"{y} 年{lvl_note}回放（GB/T 2260 快照）"}


def run_golden(path: str):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cases, by_type, passed = data["cases"], {}, 0
    for case in cases:
        exp, out = case["expect"], (resolve_by_code(case["code"]) if case.get("code") else
                                     resolve_at(case["raw"], case["year"]) if case.get("year") else resolve(case["raw"]))
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
    if mode == "at":
        if out["status"] != exp.get("status"):
            return False
        contains = exp.get("contains") or []
        if not all(c in str(out["result"] or "") for c in contains):
            return False
        nc = exp.get("now_contains")
        return (nc in str((out.get("now") or {}).get("result") or "")) if nc else True
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
