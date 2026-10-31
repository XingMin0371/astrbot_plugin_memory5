# 第三方思路与许可说明

本插件是独立实现，**没有复制下列项目的源代码**。  
这里只列出实际采用过、且不是本插件原有功能的思路；原有分层记忆、提醒、归档、加密等不列为参考点。

## 实际参考

### Memory Beyond

- 仓库：https://github.com/AlanBacker/astrbot_plugin_memory_beyond
- 许可证：MIT
- 实际参考：按会话缓存长期索引快照；召回内容追加在当前消息尾部。

### AstrNa

- 仓库：https://github.com/Sisyphbaous-DT-Project/astrbot_plugin_AstrNa
- 许可证：MIT
- 实际参考：旧图片 Base64 的清理边界；工具结果一次性压缩/归档；工具循环中途防止上下文膨胀。

### Group Chat Plus

- 仓库：https://github.com/Him666233/astrbot_plugin_group_chat_plus
- 许可证：AGPL-3.0
- 实际参考：读空气的分层过滤、注意力权重、冷群缓存自动转正。
- 说明：仅参考产品思路，没有复制其 AGPL 代码。若后续复制代码，本插件需要按 AGPL-3.0 处理。

## 许可结论

- 本插件自身继续使用 MIT。
- MIT 项目：如复制代码，保留版权与许可声明。
- AGPL 项目：只借鉴思路不构成代码复制；如复制代码，必须遵守 AGPL 的源码开放与同许可证要求。
- 本文件是致谢与合规说明，不改变本插件自身的 MIT 许可。