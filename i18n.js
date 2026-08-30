'use strict';

(function initializeI18n() {
  const STORAGE_KEY = 'axis_manager_language';
  const translations = {
    'AXIS · AI 工作站': 'AXIS · AI Workstation',
    '主导航': 'Main navigation', '语言': 'Language', '界面语言': 'Interface language', '总览': 'Overview', '工作场景': 'Scenes',
    '已登记服务': 'Registered Services', '已启动服务': 'Running Services', '资源监控': 'Resource Monitor',
    '用户管理': 'User Management', '日志中心': 'Operation Log',
    'AI 工作站': 'AI Workstation', '地址由当前部署提供': 'Address provided by this deployment',
    '系统设置': 'System Settings', '打开导航': 'Open navigation', '关闭导航': 'Close navigation', '工作站': 'Workstation',
    '界面偏好': 'Interface preferences', '显示风格': 'Display style',
    '选择适合当前工作环境的显示风格，设置只保存在这个浏览器中。': 'Choose a display style for this workstation. The setting is saved only in this browser.',
    '切换后立即应用，不影响服务、场景和资源数据。': 'Changes apply immediately and do not affect services, scenes, or resource data.',
    '矩阵绿': 'Matrix Green', '深色工程监控风格，使用清晰的荧光绿强调。': 'A dark engineering console with crisp fluorescent-green accents.',
    '极光蓝': 'Aurora Blue', '冷静的深海蓝界面，配合冰蓝与淡紫光感。': 'A calm deep-blue interface with icy blue and subtle violet light.',
    '曜石金': 'Obsidian Gold', '温暖克制的专业设备风格，以香槟金突出操作。': 'A restrained warm-black equipment style with champagne-gold actions.',
    '正在连接': 'Connecting', '刷新数据': 'Refresh data', '退出登录': 'Sign out',
    '当前工作场景': 'Current Scene', '场景检测尚未配置': 'Scene detection is not configured',
    '场景状态由低开销健康检查发现的实际状态计算。': 'Scene state is calculated from low-overhead health checks.',
    '查看场景': 'View scenes', '选择后立即切换场景': 'Switch immediately after selecting a scene',
    '切换场景': 'Switch Scene', '主机状态': 'Host status', '内存': 'Memory',
    '磁盘可用': 'Disk available', '等待数据': 'Waiting for data', '等待检测': 'Waiting for detection',
    '等待真实数据': 'Waiting for live data', '% 负载': '% load', '显存占用': 'VRAM usage',
    'GPU 负载趋势': 'GPU load trend', '最近 15 分钟': 'Last 15 minutes', '现在': 'Now',
    '登录后检测 NVIDIA GPU。': 'NVIDIA GPUs are detected after sign-in.',
    '未检测到 NVIDIA GPU。': 'No NVIDIA GPU detected.',
    '未检测到 NVIDIA GPU，当前仅显示主机资源。': 'No NVIDIA GPU detected. Only host resources are shown.',
    '查看服务': 'View services',
    '服务映射尚未配置': 'Service mapping is not configured', '只展示真实硬件指标': 'Live hardware metrics only',
    '查看只读状态': 'View saved status', '温度': 'Temperature', '功耗': 'Power', '状态': 'Status',
    '等待': 'Waiting', '开发/agent场景': 'Development / Agent scene', '服务映射': 'Service mapping',
    '未配置': 'Not configured', '等待状态': 'Waiting for status',
    '全部服务': 'All services', '登录后加载真实服务状态。': 'Sign in to load saved service states.',
    '最近事件': 'Recent events', '最近 30 分钟': 'Last 30 minutes',
    '登录后加载服务与场景操作。': 'Sign in to load service and scene operations.',
    '服务组合': 'Service combinations',
    '新场景添加在末尾；拖动卡片或使用上移、下移调整顺序。': 'New scenes are added last. Drag cards or use Move up and Move down to reorder them.',
    '添加场景': 'Add Scene', '尚未添加场景。': 'No scenes have been added.', '切换规则': 'Switching rules',
    '默认场景': 'Default Scene', '默认启动场景': 'Default Startup Scene', 'AXIS 启动时自动切换': 'AXIS switches here on startup', '当前已激活场景': 'Currently Active Scene', '服务组合正在生效': 'Service combination is active', '设为默认': 'Set as Default', '取消默认': 'Clear Default',
    '已设置默认场景': 'Default scene set', '已取消默认场景': 'Default scene cleared',
    '不执行自动回滚，最终状态和失败原因写入操作日志。': 'There is no automatic rollback. Final states and failures are written to the operation log.',
    '停止未选服务': 'Stop unselected services', '逐个调用 stop': 'Run stop one by one',
    '记录停止结果': 'Record stop results', '任一失败则不启动': 'Do not start if any stop fails',
    '启动目标服务': 'Start target services', '按场景顺序调用 start': 'Run start in scene order',
    '保存最终状态': 'Save final states', '标记激活或部分启动': 'Mark active or partially started',
    '脚本服务': 'Script services',
    '管理器直接检查本机健康接口；status 脚本只在点击“深度检查”时执行。': 'The manager checks local health endpoints directly. The status script runs only for Deep Check.',
    '停止全部服务': 'Stop All Services', '添加服务': 'Add Service',
    '搜索服务、说明、GPU 或端口': 'Search services, descriptions, GPUs, or ports',
    '全部': 'All', '已启动': 'Running', '异常': 'Unhealthy', '已停止': 'Stopped',
    '服务名称': 'Service name', '说明': 'Description', '端口': 'Port', '尚未添加服务。': 'No services have been added.',
    '持久采样 · 最近 24 小时': 'Persistent samples · Last 24 hours', '历史范围': 'History range',
    '主机资源与每张 GPU 独立分区，统一刻度展示负载、显存和关键指标。': 'Host resources and every GPU have distinct sections with consistent scales for load, VRAM, and key metrics.',
    '15 分钟': '15 minutes', '1 小时': '1 hour', '24 小时': '24 hours',
    '当前 API 尚未开放此范围': 'This range is not available in the current API',
    '登录后加载真实历史曲线。': 'Sign in to load live history charts.',
    '访问账户': 'Access accounts',
    '所有用户具有相同管理权限；新增用户仅允许从本机地址执行。': 'All users have the same administrative access. New users can only be added from the local computer.',
    '添加用户': 'Add User', '用户': 'User', '创建时间': 'Created', '活跃会话': 'Active sessions',
    '正在加载用户。': 'Loading users.', '管理记录': 'Management records',
    '只记录服务启停与场景切换的时间和结果。': 'Only service lifecycle and scene switch times and results are recorded.',
    '刷新记录': 'Refresh Log', '服务与场景操作': 'Service and Scene Operations',
    '包含每个脚本步骤、耗时和失败摘要': 'Includes every script step, duration, and failure summary',
    '尚无服务或场景操作。': 'No service or scene operations.', '安全访问': 'Secure Access',
    '正在检查管理器': 'Checking Manager', '正在确认首次设置与登录状态。': 'Checking setup and sign-in status.',
    '管理员用户名': 'Administrator username', '密码': 'Password', '确认密码': 'Confirm password',
    '继续': 'Continue', '数据已刷新': 'Data refreshed', '管理脚本绝对路径': 'Absolute management script path',
    'GPU 展示标签': 'GPU display label', '服务端口': 'Service port', 'UI 地址': 'UI address',
    '健康检查地址': 'Health check URL', '响应必须包含': 'Response must contain',
    '可选，用于区分共用端口的服务': 'Optional; distinguishes services that share a port',
    '例如 RTX 4090': 'For example, RTX 4090', '取消': 'Cancel', '保存服务': 'Save Service',
    '场景编辑器': 'Scene Editor', '场景名称': 'Scene name', '需要启动的服务': 'Services to start',
    '使用箭头调整启动顺序': 'Use the arrows to change startup order', '保存场景': 'Save Scene',
    '用户名': 'Username', '账户安全': 'Account Security', '修改密码': 'Change Password',
    '修改后，该用户的现有登录会话将全部失效。': 'All existing sessions for this user will be invalidated after the change.',
    '新密码': 'New password', '确认新密码': 'Confirm new password', '保存新密码': 'Save New Password',
    '场景切换进度': 'Scene Switch Progress', '正在切换场景': 'Switching Scene',
    '正在准备服务操作。': 'Preparing service operations.', '当前步骤': 'Current step',
    '等待管理器响应': 'Waiting for the manager', '等待第一条服务操作记录。': 'Waiting for the first service operation.',
    '终止请求会在当前脚本执行完成后生效，已经完成的服务动作不会自动回滚。': 'Cancellation takes effect after the current script finishes. Completed service actions are not rolled back.',
    '终止切换并返回': 'Cancel Switch and Return', '返回工作场景': 'Return to Scenes',
    '不支持': 'Unavailable', '未知': 'Unknown', '时间未知': 'Time unknown',
    '服务器返回了无法识别的数据。': 'The server returned unrecognized data.',
    '需要登录': 'Authentication is required.', '会话无效或已过期': 'The session is invalid or has expired.',
    'CSRF 验证失败': 'CSRF validation failed.', '用户名或密码错误': 'The username or password is invalid.',
    '用户名已存在': 'The username already exists.', '用户不存在': 'The user was not found.',
    '不能删除当前登录用户': 'The currently signed-in user cannot be deleted.',
    '不能删除最后一个用户': 'The last user cannot be deleted.',
    '登录失败次数过多，请稍后重试': 'Too many failed sign-in attempts. Try again later.',
    '新增用户仅允许从本机执行': 'This operation is only allowed from the local computer.',
    '请求参数无效': 'The request parameters are invalid.', '持久化操作失败': 'The persistent storage operation failed.',
    '服务器内部错误': 'An internal server error occurred.', '请求体超过限制': 'The request body exceeds the allowed size.',
    '已登记服务不存在': 'The registered service was not found.', '场景不存在': 'The scene was not found.',
    '操作记录不存在': 'The operation record was not found.', '已有服务或场景操作正在执行': 'A service or scene operation is already running.',
    '登录状态已过期，请重新登录。': 'Your session has expired. Sign in again.',
    '连接管理器超时。': 'The manager connection timed out.', '无法连接管理器。': 'Unable to connect to the manager.',
    '首次设置': 'Initial Setup', '创建本机管理员': 'Create Local Administrator', '登录工作站': 'Sign In',
    '首次设置仅允许在本机完成。密码至少 4 个字符。': 'Initial setup is only available locally. The password must contain at least 4 characters.',
    '使用管理员账户继续。': 'Continue with an administrator account.', '创建管理员并进入': 'Create Administrator and Continue',
    '登录': 'Sign In', '两次输入的密码不一致。': 'The passwords do not match.', '管理员': 'Admin',
    '已退出登录。': 'Signed out.', '监控离线': 'Monitoring offline', '实时': 'Live',
    '总量不可用': 'Total unavailable', '本机': 'Local host', '未检测到': 'Not detected', '已检测': 'Detected',
    '部分数据降级': 'Some data is degraded', '采集失败': 'Collection failed',
    'CPU 总负载': 'Total CPU load', '内存占用': 'Memory usage', 'GPU 负载': 'GPU load', '% 当前': '% current',
    '采样点': 'Samples', '监控设备': 'Monitored devices',
    'CPU 温度': 'CPU temperature', '内存用量': 'Memory used', '主机资源': 'Host resources',
    '处理器与系统内存使用同一条采样时间线': 'Processor and system memory share one sampling timeline',
    '处理器负载': 'Processor load', '全部逻辑处理器综合使用率': 'Combined utilization across all logical processors',
    '系统内存': 'System memory', '已用内存与物理内存容量': 'Used memory and physical-memory capacity',
    '平均': 'Average', '峰值': 'Peak', '最低': 'Minimum', '当前': 'Current', '暂无采样数据': 'No samples yet',
    '温度': 'Temperature', '功耗': 'Power', '显存用量': 'VRAM used', '独立设备遥测': 'Independent device telemetry',
    '核心负载': 'Core load', '图形与计算核心综合使用率': 'Combined graphics and compute-core utilization',
    '核心频率': 'Core clock', '功率': 'Power', '当前图形时钟': 'Current graphics clock',
    '当前 GPU 板卡功耗': 'Current GPU board power', '核心温度变化': 'GPU core temperature',
    '核心遥测相关性': 'Core telemetry correlation', '移动指针可联动对比同一时刻': 'Move the pointer to compare the same moment',
    '移动指针或使用方向键对比同一时刻': 'Move the pointer or use arrow keys to compare the same moment',
    '拖动曲线对比同一时刻': 'Drag across a chart to compare the same moment',
    '同步时间': 'Synchronized time',
    '容量与分配': 'Capacity and allocation', '选中': 'Selected',
    '每张 GPU 的核心负载、频率、功率和温度沿同一时间线对齐，移动指针即可对比关联变化。': 'Each GPU aligns core load, clock, power, and temperature on one timeline; move the pointer to compare related changes.',
    '显存占用': 'VRAM usage', '已用显存与物理显存容量': 'Used VRAM against physical capacity',
    '未检测到 NVIDIA GPU，当前仅显示主机资源。': 'No NVIDIA GPU detected. Only host resources are shown.',
    '先看整机压力，再进入 GPU、主机或系统定位瓶颈。': 'Check workstation pressure first, then open GPU, Host, or System to locate the bottleneck.',
    '监控分类': 'Monitoring sections', '总览': 'Summary', '主机': 'Host', '系统': 'System',
    '整机状态': 'Workstation status', '异常优先显示，点击项目进入详细监控': 'Problems appear first; select an item for detailed monitoring',
    '正在采样': 'Sampling', '运行正常': 'Running normally', '需要关注': 'Attention needed', '存在异常': 'Problem detected',
    '负载与温度': 'Load and temperature', '物理与提交内存': 'Physical and committed memory',
    '存储': 'Storage', '容量、吞吐与延迟': 'Capacity, throughput, and latency',
    '网络': 'Network', '上传与下载': 'Upload and download', '运行平台压力': 'Runtime platform pressure',
    'CPU 频率': 'CPU frequency', '处理器当前平均频率': 'Current average processor frequency',
    '提交内存': 'Committed memory', '系统已承诺内存与提交上限': 'Committed memory against the system commit limit',
    '页面文件': 'Page file', 'Windows 页面文件实际占用': 'Current Windows page-file usage',
    '显存控制器': 'Memory controller', '编解码': 'Encode / decode', 'GPU 运行状态': 'GPU runtime status',
    '实时状态与进程归属': 'Live state and process ownership', '风扇': 'Fan', '时钟限制': 'Clock limits', '无': 'None',
    '空闲': 'Idle', '应用时钟设置': 'Application clocks', '软件功率限制': 'Software power cap',
    '硬件降速': 'Hardware slowdown', '同步加速': 'Sync boost', '软件温度限制': 'Software thermal limit',
    '硬件温度限制': 'Hardware thermal limit', '外部功率制动': 'External power brake', '显示时钟设置': 'Display clock setting',
    '当前没有可识别的 GPU 计算进程。': 'No identifiable GPU compute process is active.',
    'WDDM 不提供进程显存': 'Per-process VRAM unavailable in WDDM',
    '另有': 'Plus', '个 GPU 进程': 'more GPU processes',
    '磁盘读取': 'Disk read', '物理磁盘读取吞吐': 'Physical-disk read throughput',
    '磁盘写入': 'Disk write', '物理磁盘写入吞吐': 'Physical-disk write throughput',
    '磁盘延迟': 'Disk latency', '每次读写操作平均等待': 'Average wait per disk operation',
    '网络下载': 'Network download', '主物理网卡接收速度': 'Primary physical adapter receive rate',
    '网络上传': 'Network upload', '主物理网卡发送速度': 'Primary physical adapter send rate',
    '网络吞吐': 'Network throughput', '主物理网卡上传与下载': 'Primary physical adapter upload and download',
    'WSL 内存': 'WSL memory', 'vmmemWSL 主机工作集': 'vmmemWSL host working set',
    'WSL Swap': 'WSL swap', '运行中 WSL 发行版交换空间': 'Swap used by the running WSL distribution',
    '虚拟化运行平台资源压力': 'Virtualized runtime resource pressure',
    '存储容量': 'Storage capacity', '逻辑卷当前用量': 'Current logical-volume usage', '存储容量不可用。': 'Storage capacity is unavailable.',
    'Docker 容器': 'Docker containers', '资源统计每 30 秒更新': 'Resource statistics update every 30 seconds',
    '没有 Docker 容器。': 'No Docker containers.', '没有运行中的 Docker 容器。': 'No Docker containers are running.', '温度不支持': 'Temperature unavailable',
    '提交内存不可用': 'Committed memory unavailable', '提交': 'Commit', '可用': 'free',
    '未运行': 'Not running', '运行中': 'Running', '已使用': 'used', '个容器运行': 'containers running',
    '状态未知': 'Unknown', '已激活': 'Active', '部分启动': 'Partially Started', '未激活': 'Inactive',
    '仅显示健康检查确认为运行中的服务': 'Only services confirmed running by health checks are shown',
    '当前没有已启动服务。': 'No services are currently running.', '没有已启动服务': 'No running services',
    'GPU 标签下没有已启动服务': 'No running service uses this GPU label',
    '尚未添加已登记服务。': 'No registered services have been added.', '无说明': 'No description',
    '打开 UI': 'Open UI', '查看': 'View', '无端口': 'No port', '尚未登记 GPU 0 服务': 'No GPU 0 services registered',
    'GPU 为用户登记标签': 'GPU is a user-provided label', '尚未登记服务': 'No service registered',
    '等待登记': 'Waiting for registration', '没有符合筛选条件的服务。': 'No services match the filters.',
    '操作中': 'In progress', '尚未检查状态': 'State has not been checked', '深度检查': 'Deep Check',
    '意外停止': 'Unexpectedly stopped', '外部启动': 'Started externally',
    '启动': 'Start', '停止': 'Stop', '重启': 'Restart', '停止失败': 'Stop failed', '；': '; ', '编辑': 'Edit', '删除': 'Delete', '未标注': 'Not specified',
    '已有操作正在执行': 'Another operation is already running', '场景顺序已保存': 'Scene order saved',
    '（当前）': ' (current)', '场景不存在，请刷新后重试': 'The scene no longer exists. Refresh and try again.',
    '拖动调整场景位置': 'Drag to reorder the scene', '此场景不启动任何服务': 'This scene starts no services',
    '打开 UI ↗': 'Open UI ↗', '上移': 'Move Up', '下移': 'Move Down', '重新切换': 'Switch Again',
    '切换到此场景': 'Switch to This Scene', '尚未添加场景': 'No scenes have been added',
    '场景未完整激活': 'No scene is fully active', '当前服务状态不完整符合任何场景。': 'The current service states do not fully match any scene.',
    '服务操作已开始': 'Service operation started', '状态检查完成': 'Status check completed',
    '停止所有已登记服务？管理器将依次调用每个服务脚本的 stop 动作。': 'Stop all registered services? The manager will run the stop action for each service in order.',
    '正在停止全部服务': 'Stopping all services', '操作成功': 'Operation succeeded', '操作已终止': 'Operation cancelled',
    '操作失败': 'Operation failed', '操作仍在后台执行，请到日志中心查看。': 'The operation is still running. Check the operation log for progress.',
    '管理器正在按顺序停止和启动服务。': 'The manager is stopping and starting services in order.',
    '等待第一项服务操作': 'Waiting for the first service operation', '正在启动': 'Starting', '正在停止': 'Stopping',
    '场景切换完成': 'Scene switch completed', '场景切换已终止': 'Scene switch cancelled', '场景切换失败': 'Scene switch failed',
    '没有需要执行的服务步骤。': 'No service steps were required.', '进行中': 'In progress', '成功': 'Succeeded',
    '已终止': 'Cancelled', '失败': 'Failed', '切换已终止': 'Switch cancelled',
    '场景切换未完成': 'Scene switch did not complete', '所有服务均已达到目标状态。': 'All services reached their target states.',
    '服务脚本执行失败': 'The service script failed.', '部分服务未能停止': 'Some services could not be stopped.',
    '用户终止了场景切换；已完成的服务动作不会自动回滚': 'The user cancelled the scene switch. Completed service actions were not rolled back.',
    '场景切换未达到全部目标状态': 'The scene switch did not reach every target state.',
    '管理器重启，操作已中断': 'The manager restarted and interrupted the operation.',
    '管理脚本必须使用绝对路径': 'The management script must use an absolute path.',
    '管理脚本只支持 .cmd、.bat、.ps1': 'Management scripts must use .cmd, .bat, or .ps1.',
    '管理脚本不存在': 'The management script was not found.',
    '未找到 PowerShell': 'PowerShell was not found.',
    '未找到 Windows 命令解释器': 'Windows Command Processor was not found.',
    '脚本动作无效': 'The script action is invalid.',
    '请在日志中心查看失败步骤。': 'Check the operation log for failed steps.', '正在提交终止请求…': 'Submitting cancellation…',
    '终止请求已提交，当前步骤结束后停止': 'Cancellation requested. Processing will stop after the current step.',
    '编辑服务': 'Edit Service', '服务已更新': 'Service updated', '服务已添加': 'Service added',
    '服务登记已删除': 'Service registration deleted', '编辑场景': 'Edit Scene', '场景已更新': 'Scene updated',
    '场景已添加': 'Scene added', '场景已删除': 'Scene deleted', '尚无用户。': 'No users found.',
    '当前登录账户': 'Current signed-in account', '管理账户': 'Administrator account', '当前用户': 'Current user',
    '可用': 'Available', '不能删除当前登录用户': 'The current user cannot be deleted',
    '不能删除最后一个用户': 'The last user cannot be deleted', '用户已添加': 'User added',
    '保存后当前会话将失效，需要使用新密码重新登录。': 'Saving will invalidate this session. Sign in again with the new password.',
    '保存后，该用户的现有登录会话将全部失效。': 'Saving will invalidate all existing sessions for this user.',
    '密码已修改，请使用新密码重新登录。': 'Password changed. Sign in with the new password.',
    '密码已修改，旧会话已失效': 'Password changed. Previous sessions are no longer valid.', '用户已删除': 'User deleted',
    '等待执行': 'Queued', '执行中': 'Running', '场景切换': 'Scene switch', '服务操作': 'Service operation',
    '暂无服务或场景操作。': 'No service or scene operations.', '场景': 'Scene', '服务': 'Service',
    '最近 30 分钟没有服务或场景操作。': 'No service or scene operations in the last 30 minutes.',
    '历史数据读取失败': 'Unable to read history data',
  };

  const templates = [
    [/^请求失败（(.+)）$/, 'Request failed ($1)'], [/^服务状态读取失败：(.+)$/, 'Unable to read service states: $1'],
    [/^历史数据读取失败：(.+)$/, 'Unable to read history data: $1'],
    [/^用户加载失败：(.+)$/, 'Unable to load users: $1'], [/^温度 (.+)$/, 'Temperature $1'],
    [/^(.+) 已使用 · 共 (.+)$/, '$1 used · $2 total'], [/^(.+) 已使用$/, '$1 used'],
    [/^(.+) 可用$/, '$1 available'], [/^(.+) 运行 · (.+) 停止$/, '$1 running · $2 stopped'],
    [/^剩余 (.+)$/, '$1 free'], [/^(.+) GPU 负载曲线$/, '$1 GPU load chart'],
    [/^状态检查于 (.+)$/, 'State checked at $1'], [/^场景 (.+)$/, 'Scene $1'],
    [/^启动顺序 (.+)$/, 'Startup order $1'], [/^包含 (.+) 个服务$/, 'Contains $1 services'],
    [/^正在切换到 (.+)$/, 'Switching to $1'], [/^正在启动 · (.+)$/, 'Starting · $1'],
    [/^正在停止 · (.+)$/, 'Stopping · $1'], [/^修改 (.+) 的密码$/, 'Change password for $1'],
    [/^拖动调整 (.+) 的位置$/, 'Drag to reorder $1'], [/^(.+) · 已激活$/, '$1 · Active'],
    [/^(.+) 已就绪$/, '$1 is ready'],
    [/^脚本退出码 (.+)$/, 'Script exit code $1'],
    [/^脚本动作 (.+) 执行超时$/, 'Script action $1 timed out'],
    [/^无法启动管理脚本: (.+)$/, 'Unable to start the management script: $1'],
    [/^删除服务“(.+)”的登记记录？原始脚本和服务不会被删除。$/, 'Delete the registration for “$1”? The original script and service will not be deleted.'],
    [/^删除场景“(.+)”？不会停止或删除任何服务。$/, 'Delete scene “$1”? No services will be stopped or deleted.'],
    [/^将“(.+)”设为默认场景？AXIS 下次启动时会自动切换到该场景。$/, 'Set “$1” as the default scene? AXIS will switch to it automatically the next time it starts.'],
    [/^删除用户“(.+)”？该用户的登录会话将立即失效。$/, 'Delete user “$1”? All sessions for this user will be invalidated immediately.'],
  ];
  const reverseTranslations = Object.fromEntries(Object.entries(translations).map(([zh, en]) => [en, zh]));
  const originalText = new WeakMap();
  const originalAttributes = new WeakMap();
  const translatedAttributes = ['title', 'placeholder', 'aria-label', 'data-title'];

  function detectedLanguage() {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'zh' || saved === 'en') return saved;
    const browserLanguage = (navigator.languages && navigator.languages[0]) || navigator.language || '';
    return browserLanguage.toLowerCase().startsWith('zh') ? 'zh' : 'en';
  }
  let language = detectedLanguage();
  function translate(value) {
    if (typeof value !== 'string') return value;
    if (language === 'zh') return reverseTranslations[value] ?? value;
    if (translations[value] !== undefined) return translations[value];
    for (const [pattern, replacement] of templates) if (pattern.test(value)) return value.replace(pattern, replacement);
    return value;
  }
  function translateTextNode(node) {
    if (node.parentElement?.closest('[data-i18n-skip]')) return;
    let record = originalText.get(node);
    if (!record || node.nodeValue !== record.output) record = { source: node.nodeValue, output: node.nodeValue };
    const source = record.source; const match = source.match(/^(\s*)(.*?)(\s*)$/s);
    const next = `${match[1]}${translate(match[2])}${match[3]}`;
    record.output = next; originalText.set(node, record);
    if (node.nodeValue !== next) node.nodeValue = next;
  }
  function translateElement(element) {
    if (element.closest('[data-i18n-skip]')) return;
    let values = originalAttributes.get(element);
    if (!values) { values = new Map(); originalAttributes.set(element, values); }
    translatedAttributes.forEach((name) => {
      if (!element.hasAttribute(name)) return;
      const current = element.getAttribute(name); let record = values.get(name);
      if (!record || current !== record.output) record = { source: current, output: current };
      const next = translate(record.source); record.output = next; values.set(name, record);
      if (element.getAttribute(name) !== next) element.setAttribute(name, next);
    });
  }
  function apply(root = document.documentElement) {
    if (root.nodeType === Node.TEXT_NODE) { translateTextNode(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
    if (root.nodeType === Node.ELEMENT_NODE) translateElement(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let node; while ((node = walker.nextNode())) node.nodeType === Node.TEXT_NODE ? translateTextNode(node) : translateElement(node);
  }
  function syncSelectors() { document.querySelectorAll('[data-language-select]').forEach((select) => { select.value = language; }); }
  function setLanguage(next) {
    if (next !== 'zh' && next !== 'en') return;
    language = next; localStorage.setItem(STORAGE_KEY, next); document.documentElement.lang = next === 'zh' ? 'zh-CN' : 'en';
    apply(); syncSelectors(); document.dispatchEvent(new CustomEvent('languagechange', { detail: { language } }));
  }
  document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
  document.addEventListener('DOMContentLoaded', () => {
    apply(); syncSelectors();
    document.querySelectorAll('[data-language-select]').forEach((select) => select.addEventListener('change', () => setLanguage(select.value)));
    new MutationObserver((records) => records.forEach((record) => {
      if (record.type === 'characterData') translateTextNode(record.target);
      else if (record.type === 'attributes') translateElement(record.target);
      else record.addedNodes.forEach(apply);
    })).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: translatedAttributes });
  });
  window.axisI18n = { apply, get language() { return language; }, setLanguage, translate };
})();
