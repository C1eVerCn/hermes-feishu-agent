"""注册12 个 VLM精标工具到 hermes-agent 注册表。

所有 handler 用 ocl.tool_guard.guarded()包裹 → L2兜底权限校验。
L1拦截由 hermes_plugins/feishu_acl钩子负责（参见 ocl/permission.TOOL_MIN_ROLE）。
"""
from tools.registry import registry

from vlm_tools import handlers
from ocl.tool_guard import guarded

_TOOLSET = "vlm"


def _reg(name, description, properties, required, handler):
 registry.register(
  name=name, toolset=_TOOLSET,
  schema={"type": "function", "function": {
   "name": name, "description": description,
   "parameters": {"type": "object", "properties": properties, "required": required},
  }},
  handler=guarded(name, handler),
 )


_reg("list_event_names", "查询所有可用的VLM精标数据场景类型(eventName)",
 {}, [], handlers.list_event_names)

_reg("list_camera_types", "查询所有可用的相机类型(cameraType),如 Front/Rear/Left/Right",
 {}, [], handlers.list_camera_types)

_reg("list_bags", "分页查询Bag包列表,支持按名称/场景/同步状态筛选。page>=1, pageSize<=100。",
 {"page": {"type": "integer", "description": "页码,默认1"},
 "pageSize": {"type": "integer", "description": "每页条数,1-100,默认10"},
 "bagName": {"type": "string", "description": "Bag名称模糊搜索(可选)"},
 "eventName": {"type": "string", "description": "场景名称精确匹配(可选)"},
 "syncStatus": {"type": "integer", "description": "同步状态:0=待处理1=成功2=失败", "enum": [0,1,2]}},
 [], handlers.list_bags)

_reg("get_bag", "根据bagId获取Bag包的完整详细信息。",
 {"bagId": {"type": "integer", "description": "Bag记录ID(路径参数)"}},
 ["bagId"], handlers.get_bag)

_reg("list_frames", "分页查询指定Bag下的帧图片列表。返回含精标JSON。",
 {"bagId": {"type": "integer", "description": "Bag记录ID(路径参数)"},
 "page": {"type": "integer", "description": "页码,默认1"},
 "pageSize": {"type": "integer", "description": "每页条数,1-100,默认10"},
 "cameraType": {"type": "string", "description": "相机类型筛选(可选,如 Front)"}},
 ["bagId"], handlers.list_frames)

_reg("get_frame", "根据frameId获取帧图片的完整详细信息(含精标JSON)。",
 {"frameId": {"type": "integer", "description": "帧图片记录ID(路径参数)"}},
 ["frameId"], handlers.get_frame)

_reg("playback_bag", "全量查询指定Bag下的所有帧图片(不分页),按帧名升序排列,用于回放场景。",
 {"bagId": {"type": "integer", "description": "Bag记录ID(路径参数)"}},
 ["bagId"], handlers.playback_bag)

_reg("download_bag_metadata", "获取Bag包的下载链接元数据(URL/fileName/contentType)。不下载二进制,只返回URL供用户浏览器直传。Bag必须已同步(syncStatus=1)。",
 {"bagId": {"type": "integer", "description": "Bag记录ID(路径参数)"}},
 ["bagId"], handlers.download_bag_metadata)

_reg("frame_image_url", "获取帧图片的访问URL元数据。不下载二进制,只返回URL供浏览器直传。帧图片必须已同步。",
 {"frameId": {"type": "integer", "description": "帧图片记录ID(路径参数)"}},
 ["frameId"], handlers.frame_image_url)

_reg("sync_execute", "扫描OSS JSON文件并解析入库(不触发资源拷贝)。调用后通常接着调用 trigger_sync_async触发异步拷贝。maxFiles限制扫描文件数。",
 {"maxFiles": {"type": "integer", "description": "最大扫描文件数,null不限制(可选)"}},
 [], handlers.sync_execute)

_reg("trigger_sync_async", "触发异步拷贝任务(从源桶拷贝到DMZ桶)。已完成时重复调用会返回错误。",
 {}, [], handlers.trigger_sync_async)

_reg("sync_status", "查询同步状态和消费者实时进度,含 bagConsumer/frameConsumer/dbStats。",
 {}, [], handlers.sync_status)

