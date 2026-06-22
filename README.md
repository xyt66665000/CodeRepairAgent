# CodeRepairAgent

对 IDA Pro / Hex-Rays / Ghidra 反编译 C 伪代码进行多阶段修复与语义恢复的工具集。

项目包含两大组件：

- **[DecompileAgent](./DecompileAgent)** — 多阶段 agent 系统，自动化执行反编译代码修复
- **[Skills](./skills)** — Claude Code 可调用的 6 个 Skill，在 Claude Code 中直接使用 (`/skill-name`)

## 快速导航

| 组件 | 说明 |
|------|------|
| [DecompileAgent](./DecompileAgent/) | 多阶段反编译修复 agent 系统 |
| [control-flow-normalizer](./skills/control-flow-normalizer/) | goto/label → 结构化控制流 |
| [decompile-repair](./skills/decompile-repair/) | 编译修复（头文件、类型、隐式声明） |
| [repair-full-pipeline](./skills/repair-full-pipeline/) | 总编排流程 |
| [restore-decompiled-structs](./skills/restore-decompiled-structs/) | 退化结构体恢复 |
| [restore-function-signatures](./skills/restore-function-signatures/) | 函数签名恢复 |
| [variable-semantic-recovery](./skills/variable-semantic-recovery/) | 变量语义恢复 |

## 管线流程

```
INPUT: 反编译 .c 文件
  │
  ▼
Phase 1: decompile-repair          — 始终执行，使代码通过严格 gcc 编译
Phase 2: restore-decompiled-structs — 恢复退化结构体，消除指针算术成员访问
Phase 3: restore-function-signatures — 恢复返回类型、参数类型/名称、调用约定
Phase 4: variable-semantic-recovery  — 恢复局部变量的语义名称和正确类型
Phase 5: control-flow-normalizer     — goto/label → while/for/switch/if-else
  │
  ▼
DONE: 可编译、语义恢复的 .c + .o
```

## 使用方式

### DecompileAgent（Agent 系统）

```bash
cd DecompileAgent
pip install -r requirements.txt
python main.py -d ./target_dir
```

### Skills（在 Claude Code 中）

在 Claude Code 会话中使用：

```
/repair-full-pipeline  # 执行完整管线
```

详细用法见各 Skill 目录下的 README。

## 许可证

MIT License
