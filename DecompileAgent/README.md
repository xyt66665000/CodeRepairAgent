# DecompileAgent

对 IDA Pro / Hex-Rays / Ghidra 反编译 C 伪代码进行**多阶段修复与语义恢复**的智能体系统。

项目与 `.claude/skills/` 下的 skill 体系完全同步，实现 `repair-full-pipeline` 总流程及其 5 个子 skill 的自动化管线。

---

## 项目架构

```
DecompileAgent/
├── main.py                              # 统一入口
├── agents/                              # Agent 模块
│   ├── __init__.py
│   ├── base_agent.py                    # 共享基础设施（ReAct 循环、工具集、上下文管理）
│   ├── pipeline_agent.py                # 总编排器（对应 repair-full-pipeline skill）
│   ├── decompile_repair_agent.py        # Phase 1：编译修复（对应 decompile-repair skill）
│   ├── struct_restore_agent.py          # Phase 2：结构体/PAMA 恢复（对应 restore-decompiled-structs skill）
│   ├── function_signature_agent.py      # Phase 3：函数签名恢复（对应 restore-function-signatures skill）
│   ├── variable_semantic_agent.py       # Phase 4：变量语义恢复（对应 variable-semantic-recovery skill）
│   ├── control_flow_agent.py            # Phase 5：控制流规范化（对应 control-flow-normalizer skill）
│   └── summary_agent.py                 # 辅助工具：函数摘要生成
├── utils/                               # 工具模块
│   ├── __init__.py
│   ├── color_print.py                   # ANSI 彩色终端输出
│   ├── logger.py                        # 统一的 JSON 日志记录
│   ├── ida_client.py                    # IDA Pro HTTP Server 客户端
│   └── format.py                        # LLM 响应中的 JSON 提取
├── ida_scripts/                         # IDA Pro 脚本
│   ├── export_all_funcs.py             # 导出全部函数伪代码（多文件/单文件）
│   ├── export_all_funcs_full.py        # 使用 decompile_many 导出（含声明）
│   └── ida_pseudocode_server.py        # IDA 伪代码实时查询 HTTP 服务器
├── .env                                 # 环境变量（API_KEY, BASE_URL, MODEL_NAME）
├── requirements.txt
└── README.md
```

---

## 管线流程（Pipeline）

```
INPUT: 反编译 .c 文件
  │
  ▼
┌─────────────────────────────────────────┐
│ Phase 1: decompile-repair               │  Gate: 始终执行
│ 使代码可通过严格 gcc 编译                │
│ 修复 #include、隐式声明、类型不匹配等     │
└─────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────┐
│ Phase 2: restore-decompiled-structs     │  Gate: PAMA 或类型退化模式
│ 恢复退化结构体，消除指针算术成员访问      │
│ *((_QWORD*)ptr+2) → ptr->field          │
└─────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────┐
│ Phase 3: restore-function-signatures    │  Gate: 泛型签名或退化调用
│ 恢复返回类型、参数类型/名称、调用约定     │
│ __int64 → 实际类型, a1→语义名称          │
└─────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────┐
│ Phase 4: variable-semantic-recovery     │  Gate: v1-vN 或 scalar-in-ptr
│ 恢复局部变量的语义名称和正确类型          │
│ v1→msg_len, _QWORD→void*                │
└─────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────┐
│ Phase 5: control-flow-normalizer        │  Gate: goto/label 模式
│ 将 goto/label 意大利面转为结构化控制流   │
│ goto→while/for/switch/if-else           │
└─────────────────────────────────────────┘
  │
  ▼
DONE: 可编译、语义恢复的 .c + .o
```

### 每阶段执行协议

```
1. Gate Check     — 扫描 .c 文件，判断触发条件是否满足
2. Backup         — cp <file>.c <file>.c.bak.phase<N>
3. Measure        — 记录文件行数/字节数（用于完整性校验）
4. Run            — 启动对应 Agent 执行修复
5. Integrity Check — 验证文件非空、行数未丢失 >20%
6. Verify         — 用严格 gcc 编译验证
   ├─ 通过 → 删除备份，进入下一阶段
   └─ 失败 → 重试（最多 3 次）
       ├─ 重试成功 → 进入下一阶段
       └─ 3 次全部失败 → ROLLBACK（恢复备份，跳过此阶段，继续管线）
```

---

## 核心设计

### 共享基础设施 (`base_agent.py`)

所有阶段 Agent 共用一套 ReAct 循环引擎：

- **ReAct 循环**：Thought → Action → Observation 迭代
- **4 个通用工具**：`Terminal`、`Read Code Slice`、`Patch Apply`、`Parse GCC Errors`
- **上下文管理**：token 超限自动压缩旧步骤为摘要
- **JSONL 步骤日志**：异步写入，完整可追溯
- **文件完整性检查**：防止 Write 截断导致的数据丢失

### 阶段 Agent

每个阶段 Agent 继承共享基础设施，通过**不同的 System Prompt** 实现专业逻辑：

| Agent | 对应 Skill | 专属能力 |
|-------|-----------|---------|
| `decompile_repair_agent` | `decompile-repair` | 值转换 vs 地址转换决策算法、缺失头文件注入 |
| `struct_restore_agent` | `restore-decompiled-structs` | 指针算术字节偏移计算、类型推断启发式、反幻觉规则 |
| `function_signature_agent` | `restore-function-signatures` | 5 维签名恢复 (D1-D5)、交叉验证优先级、UNINIT_RECOVERED_ 注入 |
| `variable_semantic_agent` | `variable-semantic-recovery` | 3 轴分析框架 (类型/名称/API)、置信度门控 |
| `control_flow_agent` | `control-flow-normalizer` | 10 种结构化模式识别、最内层优先恢复 |

### 管线编排器 (`pipeline_agent.py`)

- **固定顺序执行** Phase 1→2→3→4→5，每阶段依赖前序阶段的输出
- **Gate 机制**：Phase 1 始终执行，Phase 2-5 仅在触发条件满足时执行
- **数据决定逻辑 (数据决定逻辑)**：Phase 2 是基础——所有后续阶段依赖正确的类型信息
- **每阶段备份**：任意阶段失败可回滚，不影响管线继续
- **3 次重试上限**：防止幻觉级联——回滚优于建立在错误基础上的"修复"
- **最终报告**：汇总所有阶段的执行结果

---

## 环境配置

### 1. 创建虚拟环境并安装依赖

```bash
conda create -n CodeRepair python=3.12
conda activate CodeRepair
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env_template` 为 `.env` 文件并填入配置：

```
API_KEY=your_api_key
BASE_URL=your_base_url
MODEL_NAME=your_model_name

# 可选配置
MAX_CONTEXT_WINDOW=128000
LLM_MAX_OUTPUT_TOKENS=16384
TOOL_MAX_OUTPUT_TOKENS=32768
```

---

## 使用方式

### 完整管线

```bash
# 处理单个目录下的所有 .c 文件
python main.py -d ./target_dir

# 处理单个 .c 文件
python main.py -d ./target_dir -f test.c

# 单文件模式（目录本身就是文件所在目录）
python main.py -d ./target_dir --single
```

### 指定阶段

```bash
# 仅执行 Phase 1（编译修复）
python main.py -d ./target_dir -p 1

# 执行 Phase 1-3
python main.py -d ./target_dir -p 1,2,3

# 仅执行 Phase 4（变量语义恢复）
python main.py -d ./target_dir -f test.c -p 4
```

### 编译命令

默认使用严格 gcc 模式：

```bash
gcc -c -Werror=implicit-function-declaration -Werror=implicit-int \
    -Werror=incompatible-pointer-types -Werror=int-conversion \
    -Werror=return-type -fno-builtin -fmax-errors=0 -I.
```

### 辅助工具

```bash
# 生成函数摘要
python -m agents.summary_agent -d target_dir -f function.c
```

---

## IDA Pro 相关脚本

IDA 版本：IDA Professional 9.0+

### 导出脚本

- `export_all_funcs.py` — 导出所有函数的伪代码到单个或多个文件（CLI / GUI）
- `export_all_funcs_full.py` — 使用 `decompile_many` API 导出，含声明（CLI / GUI）

### IDA 伪代码服务器

在 IDA Pro 中运行 `ida_pseudocode_server.py`，启动 HTTP 服务，可实时查询：

- 按函数名获取完整伪代码：`GET /full?func=<name>`
- 按地址获取上下文伪代码：`GET /?ea=<addr>&n=<lines>`

**运行方式：**

- **GUI**：File > Script File > 选择 `ida_scripts/ida_pseudocode_server.py`
- **命令行**：`ida.exe -A -S"ida_pseudocode_server.py" <binary>`

---

## 阶段 Gate 触发条件速查

| Phase | 触发条件 | 扫描命令（参考） |
|-------|---------|-----------------|
| 1 | 始终执行 | — |
| 2 | `*((_QWORD*)ptr+N)`, `*((int*)ptr+N)`, `void*` + PAMA, `_UNKNOWN*` | `grep -nE '\*\(\((_QWORD\|_DWORD\|int\|void)\s*\*\s*\)' file.c` |
| 3 | `__int64` 返回/参数, `a1`-`aN` 参数名, `__fastcall`, `((ret(*)())func)()` | `grep -nE '__int64\|__fastcall\|__stdcall\|\(\(\s*\w+\s*\(\s*\*\s*\)\s*\(\s*\)\s*\)' file.c` |
| 4 | `v1`-`vN` 局部变量, `_DWORD`/`_QWORD` 在指针上下文中 | `grep -nE '\bv\d+\b' file.c` |
| 5 | `goto` 语句, `LABEL_N:` 标签 | `grep -nE '\bgoto\b\|LABEL_\d+:' file.c` |

---

## 关键规则

### Supreme Rule: Memory Semantics Above All Syntax Rules

反编译代码存在普遍的类型丢失——原本是指针的变量被识别为 `_QWORD`、`int` 等标量类型。修复时：

1. **禁止** 用 `&` 取地址来修复类型不匹配。应转换**值**：`(TargetType)(value)`，而非 `(TargetType)&value`
2. **保留** 缓冲区溢出、攻击者控制指针解引用等漏洞触发路径
3. **不改变** 任何变量的内存读写路径、解引用层级、数据流边

### 文件完整性

所有阶段 Agent 必须遵守：
- 使用 `Edit`/`Patch Apply` 进行针对性修改，避免 `Write` 全量覆盖导致截断
- 每阶段修改后验证文件非空且行数合理
