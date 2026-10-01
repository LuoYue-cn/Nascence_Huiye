# core/cognition.py
import re
import datetime
import time
import random
from collections import deque

import jieba

from .memory_engine import (
    create_memory, retrieve_similar, add_link, pathfind_activation, retrieve_by_exact_keywords, _load_memory_from_db, 
    DEFAULT_HALF_LIFE
)
from .llm_interface import decompose_input, verbalize
from .virtual_clock import clock
from .biorhythm import BIORHYTHM
from config.constants import BOT_NAME
from utils.dialogue_state import set_state, reset_state, get_state
from utils.persistence import save_state
from utils.monitor import append_log

# 不同模式的半衰期配置
MODE_HALF_LIFE = {
    "存储": 7 * 24 * 3600,   # 7天
    "询问": DEFAULT_HALF_LIFE,  # 2天
    "普通": DEFAULT_HALF_LIFE,  # 2天
}

# 不同模式的检索数量
MODE_RETRIEVAL_K = {
    "存储": 5,
    "询问": 8,
    "普通": 5,
}

# ========== 反刍抑制常量 ==========
RUMINATION_THRESHOLD = 2                            # 关键词连续出现 >= 此值，触发抑制
SEED_INHIBIT_ROUNDS = RUMINATION_THRESHOLD * 3      # 种子抑制轮数
EDGE_INHIBIT_ROUNDS = RUMINATION_THRESHOLD * 3      # 路径抑制轮数
KEYWORD_INHIBIT_ROUNDS = RUMINATION_THRESHOLD * 3   # 关键词抑制轮数，触发的关键词在此轮数内跳过检索

# ========== 关键词抽取（jieba 搜索引擎模式）==========
_KEYWORD_STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "这", "那", "这个", "那个", "这样", "那样", "什么", "怎么", "为什么", "没有",
    "可以", "知道", "觉得", "然后", "一个", "一种", "有点", "有些", "就是", "不是",
    "不会", "不要", "都会", "真的", "但是", "因为", "所以", "如果", "虽然", "而且",
    "其实", "还是", "已经", "现在", "刚才", "之后", "以前", "时候", "一次", "一下",
    "哈哈", "哈哈哈", "嘿嘿", "嘻嘻", "啊啊", "嗯嗯", "哦哦", "好的", "知道", "好吗",
}

def extract_keywords_jieba(text: str, max_keywords: int = 8) -> list:
    """用 jieba 搜索引擎模式分词，过滤停用词/单字/纯标点，返回检索关键词。

    关键词来源：上一轮的回复（心理活动或发送消息）与接收到的消息。
    """
    if not text:
        return []
    result = []
    seen = set()
    for w in jieba.cut_for_search(str(text)):
        w = w.strip()
        if len(w) < 2 or w in _KEYWORD_STOPWORDS:
            continue
        if not re.search(r"[0-9a-zA-Z\u4e00-\u9fff]", w):
            continue
        if w not in seen:
            seen.add(w)
            result.append(w)
        if len(result) >= max_keywords:
            break
    return result

# 每轮扩散记录（注入抑制时记录当前轮使用的种子和边）
_current_round_seeds: list = []
_current_round_edges: list = []

# ========== 睡眠配置 ==========
# 睡眠时机不再由固定钟点决定，改由 core.biorhythm 的睡眠压力涌现。
# 该常量保留仅为兼容旧引用，不再参与睡眠判定。
DROWSY_MARGIN = 15              # 保留字段：兼容旧引用


def _get_drowsy_memory() -> str:
    """
    返回应注入的第一人称身体感受（困倦/疲惫/精神），由生物钟精力分档生成。
    这是"内心感受"通道：让辉夜自己感到累，而不只是内部数值。
    """
    return BIORHYTHM.feeling_text()


def _strip_trailing_period(text: str) -> str:
    """去掉句末的一个句号。

    只处理末尾这一个"。"，句中的标点与其他收尾标点（？！）一律保留；
    放在入库与对话历史之前，保证记忆、历史、发送三者用的是同一份文本。
    """
    if text.endswith("。"):
        return text[:-1]
    return text

def retrieve_and_diffuse(keywords: list, max_memories: int = 10,
                         inhibited_seeds: set = None,
                         inhibited_edges: set = None) -> list:
    """
    关键词检索 + 受限BFS扩散，返回带时间标记的记忆片段列表。
    每个元素为 (real_timestamp, "[时间短语] 内容")。
    供 QQConversationService 调用。
    inhibited_seeds: 被抑制的种子ID集合，检索后从候选种子中移除
    inhibited_edges: 被抑制的边集合 (src_id, tgt_id)，扩散时跳过
    """
    global _current_round_seeds, _current_round_edges
    _current_round_seeds=[]; _current_round_edges=[]
    if not keywords:
        return []

    if inhibited_seeds is None:
        inhibited_seeds = set()
    if inhibited_edges is None:
        inhibited_edges = set()

    # 精力调制思考深度：精力越低，检索面越窄、扩散越浅（想得浅一点）
    depth = BIORHYTHM.depth_factor()
    retr_k = max(2, round(5 * depth))
    max_mem = max(4, round(max_memories * depth))
    bfs_stamina = max(1.5, 3.0 * depth)
    bfs_topk = max(4, round(8 * depth))

    # 语义检索（faiss）
    seed_ids = []
    faiss_results = []
    for kw in keywords:
        similar = retrieve_similar(kw, k=retr_k)
        for score, mem in similar:
            if mem["id"] not in seed_ids:
                seed_ids.append(mem["id"])
            faiss_results.append((score, mem))

    # 精确关键词检索（词网）
    exact_results = retrieve_by_exact_keywords(keywords, k=retr_k)
    for score, mem in exact_results:
        if mem["id"] not in seed_ids:
            seed_ids.append(mem["id"])

    # 应用种子抑制过滤
    seed_ids = [sid for sid in seed_ids if sid not in inhibited_seeds]

    # BFS扩散（同时记录本轮遍历的边）
    _current_round_edges = []
    activated_memories = []
    if seed_ids:
        activated = pathfind_activation(seed_ids, max_stamina=bfs_stamina, top_k=bfs_topk,
                                        inhibited_edges=inhibited_edges,
                                        output_visited_edges=_current_round_edges)
        activated_memories = [(mem, score) for mem, score in activated]

    # 记录本轮实际使用的种子
    _current_round_seeds = list(seed_ids)

    # 合并扩散结果 + 检索结果
    combined = {}
    for mem, score in activated_memories:
        mem_id = mem["id"]
        content = mem["content"]
        if mem_id in inhibited_seeds:
            continue
        if mem_id not in combined or score > combined[mem_id][0]:
            combined[mem_id] = (score, content, mem)

    for score, mem in faiss_results + exact_results:
        mem_id = mem["id"]
        content = mem["content"]
        if mem_id in inhibited_seeds:
            continue
        if mem_id not in combined or score > combined[mem_id][0]:
            combined[mem_id] = (score, content, mem)

    # 排序、去重、截断
    sorted_mems = sorted(combined.values(), key=lambda x: x[0], reverse=True)
    seen_texts = set()
    final_mem_objects = []
    for score, content, mem in sorted_mems:
        if content not in seen_texts:
            seen_texts.add(content)
            final_mem_objects.append(mem)
        if len(final_mem_objects) >= max_mem:
            break

    # 添加时间标记
    from utils.time_phrases import get_relative_time_phrase

    timed_memories = []
    for mem in final_mem_objects:
        virtual_ts = mem.get("creation_time", 0)
        real_ts = clock.to_real_time(virtual_ts) if mem.get("time_basis") == "unix_utc" else None
        phrase = get_relative_time_phrase(real_ts) if real_ts is not None else "时间未确认"
        timed_memories.append((real_ts, f"[{phrase}] {mem['content']}"))

    return timed_memories


def retrieve_by_concept(keywords: list, time_intent: str = "none") -> tuple:
    """概念定向检索：关键词命中概念 → 取若干事件段 → 段内按时间倒序直读。

    与向量检索的区别在于全程不做相似度排序：概念已经把话题定死，
    段内记忆又是同一次交互里的连续内容，直接按时间读即可。

    返回 (timed_memories, concept_name, degraded)：
      timed_memories 形如 [(real_ts, "[时间短语] 内容"), ...]
      concept_name 为命中的概念名；未命中时为 None
      degraded 表示时间窗内无记录、已降级为"最近若干段"
    """
    if not keywords:
        return [], None, False

    from .concept_store import match_concept, select_episodes, fetch_members
    from utils.time_phrases import get_relative_time_phrase

    # 长关键词更具体，优先用它匹配（如"物理作业"优于"作业"）
    candidates = sorted({str(k) for k in keywords if k}, key=len, reverse=True)
    cid = cname = None
    for kw in candidates:
        cid, cname, how = match_concept(kw)
        if cid:
            append_log(f"[概念路] 关键词「{kw}」命中概念「{cname}」（{how}）")
            break
    if not cid:
        return [], None, False

    try:
        episodes, degraded = select_episodes(cid, time_intent)
    except Exception as e:
        append_log(f"[概念路] 事件段选择失败，跳过: {e}")
        return [], None, False
    if not episodes:
        append_log(f"[概念路] 概念「{cname}」下没有事件段，跳过")
        return [], None, False
    if degraded:
        append_log(f"[概念路] 时间窗内无记录（意图={time_intent}），降级为最近 {len(episodes)} 段")

    try:
        members = fetch_members(episodes)
    except Exception as e:
        append_log(f"[概念路] 成员展开失败，跳过: {e}")
        return [], None, False

    timed = []
    for mem, real_ts in members:
        phrase = get_relative_time_phrase(real_ts) if real_ts is not None else "时间未确认"
        timed.append((real_ts, f"[{phrase}] {mem['content']}"))
    append_log(f"[概念路] 概念「{cname}」带回 {len(timed)} 条（{len(episodes)} 段，意图={time_intent}）")
    return timed, cname, degraded


def mask_brackets(text: str) -> str:
    """
    移除 text 中所有成对括号及其内部内容。
    支持：()、（）、[]、【】——中英文半角全角。
    未匹配的括号保留原样。
    """
    # 定义括号对（左->右）
    PAIRS = {
        '(': ')', '（': '）',
        '[': ']', '【': '】',
    }
    RIGHT_TO_LEFT = {v: k for k, v in PAIRS.items()}  # 右括号反查左括号

    stack = []          # 栈，记录每个左括号在结果串中的位置
    output = []         # 输出字符列表
    removal_ranges = [] # 待删除区间 [start, end]（含两端）

    for i, ch in enumerate(text):
        if ch in PAIRS:                     # 左括号
            stack.append(len(output))       # 记下当前位置（在 output 中的索引）
            output.append(ch)               # 暂时保留
        elif ch in RIGHT_TO_LEFT:           # 右括号
            if stack:                       # 有匹配的左括号
                left_pos = stack.pop()
                removal_ranges.append((left_pos, len(output)))
                output.append(ch)           # 暂时保留
            else:
                output.append(ch)           # 多余的右括号，保留
        else:
            output.append(ch)               # 普通字符

    # 栈中剩余未匹配的左括号位置（保留不删）
    unmatched = set(stack)

    # 构建最终结果
    result = []
    skip_until = -1
    for idx, ch in enumerate(output):
        if idx <= skip_until:
            continue
        # 检查是否在某个要删除的区间内
        removed = False
        for start, end in removal_ranges:
            if start <= idx <= end:
                skip_until = end
                removed = True
                break
        if not removed:
            result.append(ch)

    return ''.join(result)
