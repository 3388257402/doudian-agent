"""服务层包。

只放"外部世界的适配器"与纯计算：抖店客户端 / 签名 / 指标 / 通知。
与 agent 状态机相关的编排（rules / anomalies / actions / approvals / effects）同样在此目录，
但由 agent-core 维护——本包不 re-export，避免出现第二套入口（CONTRACT §1）。
"""
