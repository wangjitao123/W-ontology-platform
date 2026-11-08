"""清理项目自带的演示数据：4 个智能体 + 4 条电信场景提示词模板。

## 为什么要清

这 4 个智能体（宽带退单稽核 / 携号转网预警 / 政企根因分析 / FTTR续约）是项目
自带的**电信行业演示数据**，与当前汽车业务无关；而且它们是空壳：

    引用实体 0 个 / 引用知识库 0 个 / 工作流节点 0 个
    model=None  agent_traces 0 行  eval_suites 0 行

每个还带一个明文 api_key，留着等于 4 个可用的对外入口。

## 删什么 / 不删什么

删：
    · agents 表全部 4 行（无反向依赖：agent_traces 与 eval_suites 都是空的）
    · prompt_templates 里 4 条电信场景模板

不删：
    · 「通用本体问答助手」——它不绑定任何行业，建新智能体时还用得上

技能（技能管理）：
    · 携号转网风险评估 / 宽带退单稽核 —— 同样是电信演示数据，一并清理
    · 注意它们的 `code_ref` 指向 skill_executor.py 里的 @register_skill 内置函数，
      删的只是数据库登记行；那两段内置代码还在（它们查的是电信表，对本项目无用，
      但删代码属于改行为，不在数据清理范围内）

## 安全

先备份 ontology.db；删除按名称精确匹配，不用通配。
"""
from __future__ import annotations

import io
import sys

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 按名称精确匹配，避免误删以后新建的对象
DEMO_AGENTS = [
    "宽带退单稽核智能体",
    "携号转网预警智能体",
    "政企根因分析智能体",
    "FTTR续约策划智能体",
]

# 只删电信行业的模板；「通用本体问答助手」保留
DEMO_TEMPLATES = [
    "宽带退单稽核智能体",
    "携号转网预警智能体",
    "政企根因分析智能体",
    "FTTR续约策划智能体",
]

# 技能管理里的电信演示技能
DEMO_SKILLS = [
    "携号转网风险评估",
    "宽带退单稽核",
]

KEEP_TEMPLATE = "通用本体问答助手"


def main() -> int:
    from app.database import SessionLocal
    from app.models.agent import Agent
    from app.models.prompt_template import PromptTemplate

    db = SessionLocal()
    try:
        # ── 1) 智能体 ──
        print("=== 删除演示智能体 ===")
        removed_agents = 0
        for name in DEMO_AGENTS:
            rows = db.query(Agent).filter(Agent.name == name).all()
            for a in rows:
                print(f"  - {a.name:22} id={a.id[:8]}…  status={a.status}")
                db.delete(a)
                removed_agents += 1
        db.commit()
        print(f"  删除 {removed_agents} 个智能体")

        # ── 2) 提示词模板（保留通用那条）──
        print("=== 删除电信场景提示词模板 ===")
        removed_tpl = 0
        for name in DEMO_TEMPLATES:
            rows = (
                db.query(PromptTemplate)
                .filter(PromptTemplate.name == name)
                .all()
            )
            for t in rows:
                print(f"  - {t.name:22} id={t.id[:8]}…  category={t.category}")
                db.delete(t)
                removed_tpl += 1
        db.commit()
        print(f"  删除 {removed_tpl} 条模板")

        # ── 3) 技能（技能管理）──
        print("=== 删除电信演示技能 ===")
        from app.models.skill import Skill

        removed_skills = 0
        for name in DEMO_SKILLS:
            rows = db.query(Skill).filter(Skill.name == name).all()
            for s in rows:
                print(
                    f"  - {s.name:18} id={s.id[:8]}…  "
                    f"type={s.skill_type} code_ref={s.code_ref}"
                )
                db.delete(s)
                removed_skills += 1
        db.commit()
        print(f"  删除 {removed_skills} 个技能")

        # ── 4) 结果 ──
        print("=== 清理后 ===")
        left_agents = db.query(Agent).count()
        left_tpls = db.query(PromptTemplate).all()
        left_skills = db.query(Skill).all()
        print(f"  智能体: {left_agents} 个")
        print(f"  提示词模板: {len(left_tpls)} 条")
        for t in left_tpls:
            mark = "  ← 保留（通用）" if t.name == KEEP_TEMPLATE else ""
            print(f"    · {t.name}{mark}")
        print(f"  技能: {len(left_skills)} 个")
        for s in left_skills:
            print(f"    · {s.name}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
