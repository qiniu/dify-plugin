# Dify 插件开发指南

## 架构概览

本仓库包含**两个独立的 Dify 插件**，它们永远不应该合并：

- **ai-models-provider/**: AI 模型提供商插件（通过七牛云 API 支持 60+ LLM 模型）
- **storage-tools/**: 对象存储工具插件（文件上传/下载、存储桶管理）

每个插件都是独立的 Python 项目，拥有自己的 `manifest.yaml`、`main.py` 和 `requirements.txt`。

## 关键文件结构规则

### manifest.yaml 版本字段
```yaml
meta:
  version: 0.0.1  # ⚠️ 永远不要修改 - 插件元数据格式版本
version: 0.2.0    # ✅ 发布时更新此字段 - 插件实际版本号
```

### 模型配置（AI 模型插件）
- 模型从 API 自动生成：`scripts/update_models.py`
- 数据源：`https://openai.qiniu.com/v1/market/models`
- 文件位置：`ai-models-provider/models/llm/*.yaml` + `_position.yaml`
- 过滤规则：仅 OpenAI 协议 + 文本 LLM 模型
- 模型 ID 特殊字符转换：`deepseek/v3.2` → `deepseek-v3.2.yaml`

## 开发工作流

### 本地测试
```bash
# 在各插件目录中执行：
cd ai-models-provider  # 或 storage-tools
pip install -r requirements.txt

# 创建 .env 文件用于远程调试：
INSTALL_METHOD=remote
REMOTE_INSTALL_PORT=5003
REMOTE_INSTALL_URL=debug.dify.ai
REMOTE_INSTALL_KEY=your-key-here

python -m main
```

### 发布流程
```bash
# 交互式模式（推荐）：
./scripts/release.sh

# 直接指定模式：
./scripts/release.sh ai-models-provider 0.2.1
./scripts/release.sh storage-tools 0.2.1
```

发布脚本会执行：
1. 更新 `manifest.yaml` 版本号（仅更新底部的 `version:` 字段）
2. 提交变更：`chore(plugin-name): release version X.Y.Z`
3. 创建 Git 标签：`plugin-name-vX.Y.Z`
4. 推送以触发 GitHub Actions 发布工作流

### 更新 AI 模型列表
```bash
./scripts/update_models.py
```

脚本行为：
- **本地开发**：如果 API 数据不完整则使用默认值
- **CI 环境**：如果缺少必需字段（context_length、max_tokens）则失败
- 更新方式：全量同步（删除已移除的模型）

## 代码规范

### Commit 消息（约定式提交）

**⚠️ 重要：Commit 消息必须使用英文，遵循约定式提交规范**

```
feat: add interactive mode to release script
fix: remove tool-related logic from AI models plugin
docs: update README with repository info
refactor: split into two independent plugins
chore(ai-models-provider): release version 0.2.0
```

格式：`<type>(<scope>): <description>`
- type: feat, fix, docs, refactor, chore, test, style
- scope: 可选，如 ai-models-provider, storage-tools
- description: 简短描述，使用英文，小写开头

### Provider 实现模式

**AI 模型插件** (`ai-models-provider/provider/qiniu_ai.py`)：
- 继承 `ModelProvider`
- **凭证校验不依赖任何具体模型**：调用 `qiniu_credentials.validate_api_credentials()`，
  用一个刻意不存在的模型名（`__qiniu_dify_credentials_probe__`）发一次探针请求
- 依据是七牛云网关「**先鉴权、后路由**」：无效 key 会先返回 401，
  与请求的模型是否存在无关
- **校验是尽力而为的，绝不因外部因素阻断保存**：只有「key 为空」与
  「网关明确返回鉴权失败」会拒绝；**断网、超时、路径可疑、5xx 一律放行**（仅记警告）
- ⚠️ 历史上曾用 `deepseek-v3` 做校验模型；该模型于 2026-10-09 下架后
  **所有用户都无法保存凭证**。禁止再引入具体模型名，`tests/` 中有回归测试守护

**存储工具插件** (`storage-tools/provider/qiniu_tools.py`)：
- 继承 `ToolProvider`
- 使用 `qiniu` SDK 进行存储桶操作
- ⚠️ 凭证验证必须在网络错误时失败（不要跳过验证）

### GitHub Actions

- **runnable.yml**：在 Python 3.11/3.12 上测试两个插件，启用 pip 缓存；
  另有 `regression-tests` job 运行 `tests/`（不安装 `dify_plugin`，
  以验证校验逻辑确实与插件运行时解耦）
- **release.yml**：在 Git tag 推送时构建 `.difypkg` 文件（格式：`plugin-name-vX.Y.Z`）
- 使用 `yq v4.40.5`（版本固定以确保安全）

### 测试

```bash
python -m unittest discover -s tests -v
```

- `tests/test_qiniu_credentials.py`：凭证探针与 endpoint 规范化
- `tests/test_update_models.py`：模型下架处理、`_position.yaml` 排序、manifest 版本递增
- 只依赖 `requests` + `pyyaml`，无需安装 `dify_plugin`

## 常见陷阱

1. **不要修改 `meta.version`** - 始终保持为 `0.0.1`（注意 `bump_patch_version()`
   只匹配顶格的 `version:`，不会误改它）
2. **README 模型列表** - 保持通用描述，模型更新频繁
3. **凭证校验不阻断保存（仅 ai-models-provider）** - 除「key 为空 / 网关明确返回
   鉴权失败」外一律放行；不要把断网、超时、路径可疑当成校验失败（会再次卡住所有用户）。
   ⚠️ 与 storage-tools 策略**故意不同**：storage-tools 必须在网络错误时失败
4. **插件分离** - AI 模型 ≠ 存储工具（不同的依赖）
5. **CI 中的文件路径** - 使用 matrix 变量：`${{ matrix.plugin.path }}`
6. **禁止硬编码校验模型** - 模型会下架，校验逻辑必须与模型无关
7. **`retirement_at` 判空** - 空字符串表示**在售**；`"" <= "2026-10-15"` 为 True，
   直接做字符串比较会把所有在售模型误判为已下架
8. **`_add_custom_parameters` 仍用 `normalize_endpoint_url()`** - 即使不校验凭证，
   endpoint 规范化对运行时调用仍必要（容忍用户只填域名/多填斜杠）

## 关键文件参考

- 插件配置：`*/manifest.yaml`（注释说明版本字段）
- 发布自动化：`scripts/release.sh`（兼容 bash 3.x）
- 模型同步：`scripts/update_models.py`（API → YAML 转换器）
- CI 测试：`.github/workflows/runnable.yml`
- 构建部署：`.github/workflows/release.yml`
