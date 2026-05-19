"""
Agent 日志记录工具
提供统一的结果保存功能
"""

import os
import json
import datetime
from typing import Any, Optional, Dict


def save_agent_result(
    dir_path: str,
    result: Optional[Any] = None,
    error: Optional[Exception] = None,
    tb_str: Optional[str] = None,
    extra_log: str = "",
    agent_name: str = "agent"
) -> bool:
    """
    保存 Agent 执行结果到统一的日志文件
    
    Args:
        dir_path: 日志文件保存目录
        result: Agent 执行结果（可以是字典或其他类型）
        error: 异常对象
        tb_str: 异常堆栈跟踪字符串
        extra_log: 额外的日志信息
        agent_name: Agent 名称（用于标识）
    
    Returns:
        bool: 保存是否成功
    """
    try:
        os.makedirs(dir_path, exist_ok=True)
        json_path = os.path.join(dir_path, "agents_result.json")
        
        # 构建记录
        record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "agent_name": agent_name,
            "log_text": extra_log,
        }
        
        # 处理不同类型的结果
        if isinstance(result, dict):
            # LangChain AgentExecutor 返回的结果
            if "input" in result:
                record["input"] = result.get("input")
            if "output" in result:
                record["output"] = result.get("output")
            if "intermediate_steps" in result:
                steps = result.get("intermediate_steps")
                if isinstance(steps, list) and len(steps) > 0:
                    record["intermediate_steps"] = []
                    for action, observation in steps:
                        step = {}
                        # action 保留 tool 名称和输入
                        tool_name = getattr(action, "tool", None)
                        tool_input = getattr(action, "tool_input", None)
                        if tool_name is not None:
                            step["action"] = {"tool": tool_name}
                            if tool_input is not None:
                                step["action"]["tool_input"] = tool_input
                        # observation
                        step["observation"] = observation
                        record["intermediate_steps"].append(step)
            
            # 如果没有特殊字段，直接保存整个字典
            if "input" not in record and "output" not in record:
                record["result"] = result
        elif result is not None:
            # 其他类型的结果（字符串等）
            record["output"] = result
        
        # 错误信息
        if error is not None:
            record["error"] = str(error)
        
        # 堆栈跟踪
        if tb_str is not None:
            record["traceback"] = tb_str
        
        # 读取现有记录
        if not os.path.exists(json_path):
            existing = []
        else:
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                    if not isinstance(existing, list):
                        existing = []
            except (json.JSONDecodeError, IOError):
                existing = []
        
        # 追加新记录
        existing.append(record)
        
        # 写入文件
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        
        return True
        
    except Exception as e:
        print(f"保存日志失败: {e}")
        return False


def get_agent_results(dir_path: str, agent_name: Optional[str] = None) -> list:
    """
    读取 Agent 执行结果
    
    Args:
        dir_path: 日志文件目录
        agent_name: 可选，筛选特定 Agent 的结果
    
    Returns:
        list: 结果记录列表
    """
    json_path = os.path.join(dir_path, "agents_result.json")
    
    if not os.path.exists(json_path):
        return []
    
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            results = json.load(f)
            if not isinstance(results, list):
                return []
            
            # 如果指定了 agent_name，则过滤
            if agent_name:
                results = [r for r in results if r.get("agent_name") == agent_name]
            
            return results
    except (json.JSONDecodeError, IOError):
        return []