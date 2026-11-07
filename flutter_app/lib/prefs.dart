import 'dart:convert';
import 'dart:io';

/// 本地偏好：写入 %APPDATA%/super-brain/flutter_prefs.json（零第三方依赖）。
class Prefs {
  static Map<String, dynamic> _cache = <String, dynamic>{};

  static File get _file {
    final base = Platform.isWindows
        ? (Platform.environment['APPDATA'] ?? Directory.current.path)
        : (Platform.environment['HOME'] ?? Directory.current.path);
    final dir = Directory('$base${Platform.pathSeparator}super-brain');
    if (!dir.existsSync()) dir.createSync(recursive: true);
    return File('${dir.path}${Platform.pathSeparator}flutter_prefs.json');
  }

  static Map<String, dynamic> load() {
    try {
      final file = _file;
      if (file.existsSync()) {
        _cache = jsonDecode(file.readAsStringSync()) as Map<String, dynamic>;
      }
    } catch (_) {
      _cache = <String, dynamic>{};
    }
    _cache.putIfAbsent('host', () => '127.0.0.1');
    _cache.putIfAbsent('http_port', () => 8768);
    _cache.putIfAbsent('ws_port', () => 8767);
    _cache.putIfAbsent('auto_start_backend', () => true);
    _cache.putIfAbsent('backend_command', () => '');
    _cache.putIfAbsent('backend_dir', () => '');
    _cache.putIfAbsent('backend_args', () => '-m headless_server');
    return _cache;
  }

  static T get<T>(String key, T fallback) {
    final value = _cache[key];
    return value is T ? value : fallback;
  }

  static void save(Map<String, dynamic> values) {
    _cache = <String, dynamic>{..._cache, ...values};
    try {
      _file.writeAsStringSync(const JsonEncoder.withIndent('  ').convert(_cache));
    } catch (_) {
      // 只读环境下退化为内存偏好，不影响运行
    }
  }
}
