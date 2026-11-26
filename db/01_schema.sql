-- 校准参数 2PC 恢复演示库结构（PostgreSQL 14+）
-- 该文件同时用于：
--   1) docker-compose 首次初始化（挂载到 /docker-entrypoint-initdb.d）
--   2) 应用启动时幂等建表（CREATE TABLE IF NOT EXISTS）

-- 当前生效的校准参数
CREATE TABLE IF NOT EXISTS calibration_param (
    param_no      text PRIMARY KEY,          -- 参数号（按号查询）
    device_no     text NOT NULL,             -- 所属设备号
    param_name    text NOT NULL,             -- 参数名称
    param_value   text NOT NULL,             -- 当前参数值（text 承载不同量纲）
    unit          text NOT NULL DEFAULT '',
    version       integer NOT NULL DEFAULT 1,
    updated_gxid  text,                      -- 最近一次改动的全局事务号
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- 发布单：在 PREPARE TRANSACTION 之前以独立事务写入。
-- 因此即使预备事务最终回滚，发布单与“前像值”仍然存在，是判断回滚的重要证据。
CREATE TABLE IF NOT EXISTS publish_order (
    gxid           text PRIMARY KEY,         -- 全局事务号 / 预备事务标识符(gid)
    param_no       text NOT NULL REFERENCES calibration_param(param_no),
    intended_value text NOT NULL,            -- 本次拟发布的目标值
    baseline_value text NOT NULL,            -- 发布前的基线值（前像）
    reason         text NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- 参数修订证据：由预备事务写入，只有 COMMIT PREPARED 后才对其他会话可见。
-- gxid 唯一约束同时保证一个事务至多留下一条修订（数据库侧幂等）。
CREATE TABLE IF NOT EXISTS param_revision (
    id          bigserial PRIMARY KEY,
    gxid        text NOT NULL UNIQUE,
    param_no    text NOT NULL,
    param_value text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- 提交标记：同样只在预备事务内部插入，是“已提交”的强证据。
CREATE TABLE IF NOT EXISTS txn_commit_marker (
    gxid         text PRIMARY KEY,
    committed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_publish_order_param ON publish_order(param_no);
CREATE INDEX IF NOT EXISTS idx_revision_param ON param_revision(param_no);

-- 内置参数发布样例（幂等播种）
INSERT INTO calibration_param (param_no, device_no, param_name, param_value, unit, version, updated_gxid)
VALUES
    ('CAL-T-1001', 'DEV-T-01', '温度探头零点偏移', '0.1250', '℃', 3, NULL),
    ('CAL-P-2002', 'DEV-P-07', '压力传感器量程系数', '1.00320', 'MPa', 5, NULL),
    ('CAL-V-3003', 'DEV-V-12', '电压通道增益修正',   '0.99875', 'V/V', 2, NULL),
    -- 发布台名额演示：普通设备集中发布争抢的同一参数行
    ('CAL-BULK-1001', 'DEV-BULK-01', '集中发布批次·主控回路参数', '2.4000', 'ratio', 7, NULL),
    -- 抢修设备自有参数行（与集中发布不冲突，便于演示“及时更新”）
    ('CAL-EMRG-0001', 'DEV-EMG-01', '抢修终端·现场校准系数', '8.8880', 'kV', 4, NULL)
ON CONFLICT (param_no) DO NOTHING;
