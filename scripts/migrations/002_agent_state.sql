-- ============================================================
-- 002_agent_state.sql — Agent 状态管理 v4.2
-- ============================================================
-- agents 表增加 user_preferences / extra 列，支持 Supervisor
-- 统一管理 Agent 完整生命周期状态。
-- ============================================================

ALTER TABLE agents ADD COLUMN IF NOT EXISTS user_preferences JSONB DEFAULT '{}';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS extra JSONB DEFAULT '{}';

COMMENT ON COLUMN agents.user_preferences IS '用户偏好设置 (JSONB): {research_domain, writing_style, language_pref, mentor_quotes, other}';
COMMENT ON COLUMN agents.extra IS '扩展配置 (JSONB): {checkpoint_backend, iteration_limit, user_timeout_seconds, ...}';
COMMENT ON COLUMN agents.state IS 'Agent 状态: pending|starting|idle|busy|stopping|stopped|crashed|stalled';
