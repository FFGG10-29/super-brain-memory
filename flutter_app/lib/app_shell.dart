import 'dart:async';

import 'package:flutter/material.dart';

import 'analytics.dart';
import 'backend_launcher.dart';
import 'brain_client.dart';
import 'ai_space_page.dart';
import 'cache_page.dart';
import 'model_providers_page.dart';
import 'chat_page.dart';
import 'context_pages.dart';
import 'narrative_page.dart';
import 'pool_page.dart';
import 'nebula_page.dart';
import 'pages.dart';
import 'pages_extra.dart';
import 'persona_page.dart';
import 'prefs.dart';
import 'system_features.dart';
import 'system_gauges.dart';
import 'win32.dart';

/// LivingMemory 风格外壳：十组主导航（29 页）+ 顶栏（空间/搜索/实时仪表/导出/全屏）+ 底部状态栏。
class AppShell extends StatefulWidget {
  const AppShell({super.key});

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  late BrainClient _client;
  final _nebulaKey = GlobalKey<NebulaPageState>();
  final TextEditingController _globalSearch = TextEditingController();
  Timer? _statusTimer;
  StreamSubscription<Map<String, dynamic>>? _eventsSub;
  bool _trayRequested = false;

  int _index = 0;
  int _groupIndex = 0;
  // 切页计数：只有当前页的 key 随之变化，达到「切过去就重新拉数据」的效果
  int _pageEpoch = 0;
  bool _fullscreen = false;
  String _space = '全部';
  String _inspectorTarget = '';
  List<String> _spaces = <String>['全部', '情感记忆'];
  Map<String, dynamic> _status = <String, dynamic>{};

  static const int _nebulaIndex = 0;
  static const int _timelineIndex = 13;
  static const int _inspectorIndex = 12;

  static const List<Map<String, Object>> _nav = <Map<String, Object>>[
    <String, Object>{'name': '星云视图', 'icon': Icons.blur_on},
    <String, Object>{'name': '记忆管理', 'icon': Icons.memory},
    <String, Object>{'name': '召回测试', 'icon': Icons.search},
    <String, Object>{'name': '硬件仪表', 'icon': Icons.speed},
    <String, Object>{'name': 'PAD 认知分区', 'icon': Icons.scatter_plot_outlined},
    <String, Object>{'name': '2D 语义环', 'icon': Icons.hub_outlined},
    <String, Object>{'name': '系统概览', 'icon': Icons.monitor_heart},
    <String, Object>{'name': '展示数据', 'icon': Icons.data_object},
    <String, Object>{'name': '模型配置', 'icon': Icons.smart_toy_outlined},
    <String, Object>{'name': '接口与端口', 'icon': Icons.settings_ethernet},
    <String, Object>{'name': '同步与备份', 'icon': Icons.cloud_sync},
    <String, Object>{'name': '设置', 'icon': Icons.tune},
    <String, Object>{'name': '记忆检查器', 'icon': Icons.fact_check_outlined},
    <String, Object>{'name': '事件时间轴', 'icon': Icons.timeline},
    <String, Object>{'name': '批量导入', 'icon': Icons.upload_file},
    <String, Object>{'name': '全量配置', 'icon': Icons.tune_outlined},
    <String, Object>{'name': '桌面能力', 'icon': Icons.desktop_windows_outlined},
    <String, Object>{'name': '关于', 'icon': Icons.info_outline},
    <String, Object>{'name': '上下文状态', 'icon': Icons.dashboard_outlined},
    <String, Object>{'name': '运行时记忆', 'icon': Icons.article_outlined},
    <String, Object>{'name': '项目档案', 'icon': Icons.folder_copy_outlined},
    <String, Object>{'name': '记忆空间', 'icon': Icons.workspaces_outline},
    <String, Object>{'name': '技能管理', 'icon': Icons.auto_awesome_outlined},
    <String, Object>{'name': '人格设定', 'icon': Icons.pets_outlined},
    <String, Object>{'name': '叙事洞察', 'icon': Icons.auto_stories},
    <String, Object>{'name': '大肥鱼聊天', 'icon': Icons.chat_bubble_outline},
    <String, Object>{'name': '号池网关', 'icon': Icons.dns_outlined},
    <String, Object>{'name': '缓存监控', 'icon': Icons.speed_outlined},
    <String, Object>{'name': 'AI 空间', 'icon': Icons.person_outline},
    <String, Object>{'name': '供应商路由', 'icon': Icons.alt_route},
  ];

  /// 主导航分组：左栏只显示 7 个入口，组内子页放到顶栏下方的子导航条。
  /// 值仍是上面的全局页面索引，_index / IndexedStack / 各跳转常量完全不用改。
  static const List<Map<String, Object>> _groups = <Map<String, Object>>[
    <String, Object>{'name': '星云视图', 'icon': Icons.blur_on, 'pages': <int>[0]},
    <String, Object>{'name': '人格设定', 'icon': Icons.pets_outlined, 'pages': <int>[23]},
    <String, Object>{'name': '记忆工作台', 'icon': Icons.memory, 'pages': <int>[1, 2, 14]},
    <String, Object>{'name': '记忆洞察', 'icon': Icons.insights_outlined, 'pages': <int>[4, 5, 12, 13, 24, 28]},
    <String, Object>{'name': 'Agent 上下文', 'icon': Icons.psychology_outlined, 'pages': <int>[18, 19, 20, 21, 22]},
    <String, Object>{'name': '模型配置', 'icon': Icons.smart_toy_outlined, 'pages': <int>[8, 26, 29]},
    <String, Object>{'name': '大肥鱼聊天', 'icon': Icons.chat_bubble_outline, 'pages': <int>[25]},
    <String, Object>{'name': '运行监控', 'icon': Icons.monitor_heart, 'pages': <int>[3, 6, 7, 8, 9, 27]},
    <String, Object>{'name': '系统设置', 'icon': Icons.tune, 'pages': <int>[11, 15, 16, 10]},
    <String, Object>{'name': '关于', 'icon': Icons.info_outline, 'pages': <int>[17]},
  ];

  static List<int> _pagesOfGroup(int group) =>
      _groups[group.clamp(0, _groups.length - 1)]['pages']! as List<int>;

  static int _groupOf(int page) {
    for (var g = 0; g < _groups.length; g++) {
      if (_pagesOfGroup(g).contains(page)) return g;
    }
    return 0;
  }

  @override
  void initState() {
    super.initState();
    final prefs = Prefs.load();
    _client = BrainClient(
      host: prefs['host']?.toString() ?? '127.0.0.1',
      httpPort: prefs['http_port'] as int? ?? 8768,
      wsPort: prefs['ws_port'] as int? ?? 8767,
    );
    _subscribe();
    _client.connectEvents();
    _loadSpaces();
    _refreshStatus();
    _statusTimer = Timer.periodic(const Duration(seconds: 5), (_) => _refreshStatus());
  }

  void _subscribe() {
    _eventsSub?.cancel();
    _eventsSub = _client.events.listen(_onEvent);
  }

  void _onEvent(Map<String, dynamic> event) {
    final type = event['type']?.toString() ?? '';
    if (type == 'connected' && !_trayRequested) {
      _trayRequested = true;
      unawaited(_enableTray());
      return;
    }
    if (type != 'tray_action') return;
    final action = event['action']?.toString() ?? '';
    trayStatus.value = '托盘动作：$action';
    switch (action) {
      case 'open':
        Win32Window.show();
      case 'nebula':
        Win32Window.show();
        _goTo(_nebulaIndex);
      case 'timeline':
        Win32Window.show();
        _goTo(_timelineIndex);
      case 'quit':
        unawaited(_quitFully());
    }
  }

  /// 托盘「完全退出」：先让后端优雅停机，再关前端窗口。
  /// 顺序不能反——否则后端还占着 Kùzu/LMDB 锁，前端已消失就没人收尾了。
  Future<void> _quitFully() async {
    try {
      await _client.setTray(false);
    } catch (_) {}
    await _client.shutdownBackend();
    await BackendLauncher.stopOwned();
    Win32Window.quit();
  }

  Future<void> _enableTray() async {
    try {
      final result = await _client.setTray(true);
      final ok = result['ok'] == true || result['running'] == true;
      // 「关窗隐藏到托盘」默认关闭：Windows 11 常把新托盘图标折叠到溢出区，
      // 用户看不到图标时一旦隐藏窗口就再也找不回来（表现为「软件打不开」）。
      // 只有用户在「设置 → 诊断」里确认托盘图标可见并手动开启，才启用隐藏模式。
      final hideOnClose = Prefs.get<bool>('hide_on_close', false);
      trayStatus.value = ok
          ? (hideOnClose
              ? '托盘已启用 · 关窗隐藏到托盘（可在设置 → 诊断关闭）'
              : '托盘已启用 · 关窗直接退出（隐藏已关闭，避免找不到窗口）')
          : '托盘不可用：${result['message'] ?? result['error'] ?? '未知原因'}';
      Win32Window.setTrayMode(ok && hideOnClose);
    } on Object catch (error) {
      trayStatus.value = '托盘启用失败：$error';
      Win32Window.setTrayMode(false);
    }
  }

  Future<void> _loadSpaces() async {
    final data = await _client.spaces();
    if (!mounted) return;
    final tiers = (data['tiers'] as Map<String, dynamic>? ?? <String, dynamic>{}) .keys.toList();
    final spaces = <String>['全部', '情感记忆', ...tiers].toSet().toList();
    setState(() {
      _spaces = spaces;
      if (!spaces.contains(_space)) _space = '全部';
    });
  }

  Future<void> _refreshStatus() async {
    final data = await _client.snapshot();
    if (mounted) setState(() => _status = data);
  }

  void _reconnect(String host, int httpPort, int wsPort) {
    _client.dispose();
    setState(() {
      _client = BrainClient(host: host, httpPort: httpPort, wsPort: wsPort);
      _index = 0;
      _groupIndex = 0;
      _trayRequested = false;
    });
    _subscribe();
    _client.connectEvents();
  }

  /// 统一切页入口：递增 epoch，让目标页重建并重新拉数据。
  /// 再次点击当前页也会刷新（手动强刷）。
  void _goTo(int index) {
    if (!mounted) return;
    setState(() {
      _index = index;
      _groupIndex = _groupOf(index);
      _pageEpoch++;
    });
  }

  /// 只有「当前活跃页」的 key 带 epoch：切页时仅重建目标页，
  /// 其余页面 State 保留（不丢滚动位置与输入内容）。
  List<Widget> _pagesWithKeys() {
    final pages = <Widget>[
      NebulaPage(key: _nebulaKey, client: _client),
      MemoriesPage(client: _client),
      RecallAnalyticsPage(client: _client, onOpenMemory: _openMemory, onNebulaSearch: _nebulaSearch),
      HardwarePage(client: _client),
      PadAnalyticsPage(client: _client, onOpenMemory: _openMemory),
      Graph2DPage(client: _client, onOpenMemory: _openMemory, onNebulaSearch: _nebulaSearch),
      SystemPage(client: _client),
      DataPage(client: _client),
      ModelPage(client: _client),
      InterfacePage(client: _client),
      SyncPage(client: _client),
      SettingsPage(client: _client, onReconnect: _reconnect),
      MemoryInspectorPage(
        key: ValueKey<String>(_inspectorTarget),
        client: _client,
        initialId: _inspectorTarget,
      ),
      TimelinePage(client: _client),
      ImportPage(client: _client),
      FullConfigPage(client: _client),
      SystemFeaturesPage(client: _client),
      const AboutPage(),
      ContextStatusPage(client: _client),
      RuntimeMemoryPage(client: _client),
      DocumentsPage(client: _client),
      MemorySpacesPage(client: _client),
      SkillsPage(client: _client),
      PersonaPage(client: _client),
      NarrativeInsightPage(client: _client),
      ChatPage(client: _client, onOpenSpace: () => _goTo(28)),
      PoolPage(client: _client),
      CachePage(client: _client),
      AiSpacePage(client: _client),
      ModelProvidersPage(client: _client),
    ];
    return <Widget>[
      for (var i = 0; i < pages.length; i++)
        KeyedSubtree(
          key: ValueKey<String>(i == _index ? 'active-$i-$_pageEpoch' : 'idle-$i'),
          child: pages[i],
        ),
    ];
  }

  /// 2D 语义环 / 召回归因 / PAD 散点 → 直接跳到检查器并选中该记忆。
  void _openMemory(String id) {
    if (id.isEmpty) return;
    _inspectorTarget = id;
    _goTo(_inspectorIndex);
  }

  /// 2D / 召回页 → 切到 3D 星云并触发检索光脉冲（同一份主库数据）。
  void _nebulaSearch(String query) {
    if (query.trim().isEmpty) return;
    _goTo(_nebulaIndex);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _nebulaKey.currentState?.search(query.trim());
    });
  }

  Future<void> _export() async {
    final result = await _client.exportAll();
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(result['error']?.toString() ?? '已导出到 ${result['dashboard']}'),
      duration: const Duration(seconds: 3),
    ));
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    _eventsSub?.cancel();
    _client.dispose();
    _globalSearch.dispose();
    SingleInstance.release();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final engines = (_status['engine_health']?['engines'] as List<dynamic>? ?? <dynamic>[]);
    final ok = engines.where((row) => (row as Map<String, dynamic>)['ok'] != false).length;
    final groupPages = _pagesOfGroup(_groupIndex);
    return Scaffold(
      backgroundColor: const Color(0xFF05080E),
      body: Row(
        children: <Widget>[
          if (!_fullscreen)
            Container(
              width: 220,
              color: const Color(0xFF070D18),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  const SizedBox(height: 24),
                  const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 18),
                    child: Text('LIVING MEMORY', style: TextStyle(color: Color(0xFFE5BC85), fontSize: 14, fontWeight: FontWeight.bold, letterSpacing: 1.5)),
                  ),
                  const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 18, vertical: 4),
                    child: Text('super-brain v3 · 3D 星云 + 2D 控制台', style: TextStyle(color: Color(0xFF5D7690), fontSize: 10)),
                  ),
                  const SizedBox(height: 16),
                  Expanded(
                    child: ListView.builder(
                      itemCount: _groups.length,
                      itemBuilder: (context, g) => _NavItem(
                        icon: _groups[g]['icon']! as IconData,
                        label: _groups[g]['name']! as String,
                        selected: _groupIndex == g,
                        onTap: () => _goTo(_pagesOfGroup(g).first),
                      ),
                    ),
                  ),
                  const Padding(
                    padding: EdgeInsets.all(14),
                    child: Text('主导航 10 组 · 组内子页见顶部\n拖拽翻滚 · 滚轮缩放 · 双击复位',
                        style: TextStyle(color: Color(0xFF3E5470), fontSize: 10, height: 1.6)),
                  ),
                ],
              ),
            ),
          Expanded(
            child: Column(
              children: <Widget>[
                // ── 顶栏（含 CPU / 内存 / 显卡 实时仪表）───────────────
                Container(
                  height: 52,
                  padding: const EdgeInsets.symmetric(horizontal: 18),
                  decoration: const BoxDecoration(border: Border(bottom: BorderSide(color: Color(0xFF142238)))),
                  child: Row(
                    children: <Widget>[
                      DropdownButton<String>(
                        value: _space,
                        dropdownColor: const Color(0xFF0A1424),
                        style: const TextStyle(color: Color(0xFFBFD8F5), fontSize: 11),
                        underline: const SizedBox.shrink(),
                        items: _spaces.map((space) => DropdownMenuItem<String>(
                              value: space, child: Text(space))).toList(),
                        onChanged: (value) {
                          if (value == null) return;
                          setState(() => _space = value);
                          _nebulaKey.currentState?.applySpace(value);
                        },
                      ),
                      const SizedBox(width: 10),
                      IconButton(
                        tooltip: '返回全景',
                        icon: const Icon(Icons.home_outlined, color: Color(0xFF8FA6C4), size: 18),
                        onPressed: () => _nebulaKey.currentState?.resetView(),
                      ),
                      IconButton(
                        tooltip: '刷新当前页数据',
                        icon: const Icon(Icons.refresh, color: Color(0xFF8FA6C4), size: 18),
                        onPressed: () => _goTo(_index),
                      ),
                      Expanded(
                        child: TextField(
                          controller: _globalSearch,
                          style: const TextStyle(color: Colors.white, fontSize: 12),
                          decoration: const InputDecoration(
                            hintText: '搜索记忆并激活星爆…',
                            hintStyle: TextStyle(color: Color(0xFF63788F), fontSize: 11),
                            border: InputBorder.none,
                            prefixIcon: Icon(Icons.search, color: Color(0xFF4FC3F7), size: 16),
                          ),
                          onSubmitted: (value) {
                            _goTo(_nebulaIndex);
                            _nebulaKey.currentState?.search(value);
                          },
                        ),
                      ),
                      SystemGauges(client: _client),
                      const VerticalDivider(color: Color(0xFF142238), width: 20),
                      IconButton(tooltip: '导出数据', icon: const Icon(Icons.download_outlined, color: Color(0xFF8FA6C4), size: 18), onPressed: _export),
                      IconButton(
                        tooltip: '全屏',
                        icon: const Icon(Icons.fullscreen, color: Color(0xFF8FA6C4), size: 18),
                        onPressed: () => setState(() => _fullscreen = !_fullscreen),
                      ),
                      const SizedBox(width: 6),
                      const Icon(Icons.circle, color: Color(0xFF3DDC84), size: 7),
                      const SizedBox(width: 6),
                      Text('${_client.host}:${_client.httpPort}', style: const TextStyle(color: Color(0xFF6B839F), fontSize: 10)),
                    ],
                  ),
                ),
                // ── 子导航：当前主导航组内的页面（单页组不显示）──────────
                if (groupPages.length > 1)
                  Container(
                    height: 40,
                    padding: const EdgeInsets.symmetric(horizontal: 18),
                    decoration: const BoxDecoration(
                      color: Color(0xFF080F1C),
                      border: Border(bottom: BorderSide(color: Color(0xFF142238))),
                    ),
                    child: ListView.separated(
                      scrollDirection: Axis.horizontal,
                      itemCount: groupPages.length,
                      separatorBuilder: (_, __) => const SizedBox(width: 8),
                      itemBuilder: (context, i) {
                        final page = groupPages[i];
                        final selected = page == _index;
                        return Center(
                          child: ChoiceChip(
                            label: Text(
                              _nav[page]['name']! as String,
                              style: TextStyle(
                                fontSize: 11,
                                color: selected ? const Color(0xFF06121F) : const Color(0xFF8FA6C4),
                              ),
                            ),
                            selected: selected,
                            selectedColor: const Color(0xFF4FC3F7),
                            backgroundColor: const Color(0xFF0E1B2D),
                            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
                            onSelected: (_) => _goTo(page),
                          ),
                        );
                      },
                    ),
                  ),
                // ── 主区 ────────────────────────────────────────────────
                Expanded(
                  child: IndexedStack(
                    // 重连换 client 后整棵子树重建，各页面才会用新 client 重新拉数据
                    key: ValueKey<int>(_client.hashCode),
                    index: _index,
                    children: _pagesWithKeys(),
                  ),
                ),
                // ── 底部状态栏 ──────────────────────────────────────────
                Container(
                  height: 28,
                  padding: const EdgeInsets.symmetric(horizontal: 18),
                  color: const Color(0xFF070D18),
                  child: Row(
                    children: <Widget>[
                      Text(
                        '引擎 ${engines.isEmpty ? '—' : '$ok/${engines.length}'} · '
                        '记忆 ${_status['memory_stream']?['count'] ?? 0} · '
                        '空间 $_space · 待重放 ${_status['sync_status']?['pending_events'] ?? 0}',
                        style: const TextStyle(color: Color(0xFF6B839F), fontSize: 10),
                      ),
                      const Spacer(),
                      ValueListenableBuilder<String>(
                        valueListenable: trayStatus,
                        builder: (context, value, _) => Padding(
                          padding: const EdgeInsets.only(right: 14),
                          child: Text(value, style: const TextStyle(color: Color(0xFF3E5470), fontSize: 10)),
                        ),
                      ),
                      ValueListenableBuilder<String>(
                        valueListenable: backendStatus,
                        builder: (context, value, _) => Text(
                          'WebSocket RPC · $value',
                          style: const TextStyle(color: Color(0xFF3E5470), fontSize: 10),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _NavItem extends StatelessWidget {
  const _NavItem({required this.icon, required this.label, required this.selected, required this.onTap});
  final IconData icon;
  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => InkWell(
        onTap: onTap,
        child: Container(
          margin: const EdgeInsets.symmetric(horizontal: 10, vertical: 2),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: selected ? const Color(0xFF123049) : Colors.transparent,
            borderRadius: BorderRadius.circular(8),
          ),
          child: Row(
            children: <Widget>[
              Icon(icon, size: 16, color: selected ? const Color(0xFF4FC3F7) : const Color(0xFF6B839F)),
              const SizedBox(width: 12),
              Text(label, style: TextStyle(color: selected ? Colors.white : const Color(0xFF8FA6C4), fontSize: 12)),
            ],
          ),
        ),
      );
}
