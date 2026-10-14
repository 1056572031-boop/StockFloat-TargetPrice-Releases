# -*- coding: utf-8 -*-
"""目标价生命周期协调器：只处理状态转换与持久化事务，不依赖 Qt。"""

from targets import drop_expired, merge_alerts


class TargetLifecycleCoordinator:
    """将目标价保存和清空从主窗口的 Qt 编排中分离。"""

    def __init__(self, save_alerts, now):
        self._save_alerts = save_alerts
        self._now = now

    def save(self, desired, live):
        """合并并清理目标价；仅在内容变化时落盘。

        返回 ``(ok, alerts, changed)``。保存失败时 alerts 保持 live，确保内存与磁盘一致。
        """
        # 过滤非 dict 项：merge_alerts/drop_expired 均假设元素是 dict，
        # 历史配置或外部写入可能混入脏数据。
        live = [a for a in (live or []) if isinstance(a, dict)]
        desired = [a for a in (desired or []) if isinstance(a, dict)]
        merged = drop_expired(merge_alerts(desired, live, now=self._now()), self._now())
        # 早返回：内容没变就不写盘，省掉无谓磁盘 IO，也不依赖配置文件当前可写。
        if merged == live:
            return True, live, False
        if not self._save_alerts(merged):
            return False, live, False
        return True, merged, True

    def clear(self, live):
        """清空已有目标价；空状态不写盘，避免无谓依赖配置文件可写。"""
        live = [a for a in (live or []) if isinstance(a, dict)]
        if not live:
            return True, [], False
        if not self._save_alerts([]):
            return False, live, False
        return True, [], True
