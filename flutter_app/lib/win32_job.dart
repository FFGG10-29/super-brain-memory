import 'dart:ffi' as ffi;
import 'dart:io';

/// Windows Job Object：把前端进程与它拉起的所有子进程绑进同一个 job，并设置
/// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE。前端一旦消失（正常退出、被强杀、崩溃），
/// job 的最后一个句柄由系统关闭，后端会被一并终止——杜绝「前端退出了，Python
/// Daemon 还在后台吃内存 / 把 Kùzu 数据库锁死」的残留问题。
///
/// 零 pub 依赖：只用 dart:ffi 调 kernel32 + msvcrt 的 malloc/free。
/// 子进程默认继承父进程的 job 成员资格（Dart 的 ProcessStartMode.detached
/// 不会加 CREATE_BREAKAWAY_FROM_JOB），所以 BackendLauncher 拉起的后端自动入组。
class Win32Job {
  Win32Job._();

  static const int _jobObjectExtendedLimitInformation = 9;
  static const int _killOnJobClose = 0x2000;
  // x64: JOBOBJECT_BASIC_LIMIT_INFORMATION(64) + IO_COUNTERS(48) + 4*SIZE_T(32)
  static const int _infoSize = 144;

  static final ffi.DynamicLibrary _k32 = ffi.DynamicLibrary.open('kernel32.dll');
  static final ffi.DynamicLibrary _c = ffi.DynamicLibrary.open('msvcrt.dll');

  static final _malloc = _c.lookupFunction<
      ffi.Pointer<ffi.Void> Function(ffi.UintPtr),
      ffi.Pointer<ffi.Void> Function(int)>('malloc');
  static final _free = _c.lookupFunction<
      ffi.Void Function(ffi.Pointer<ffi.Void>),
      void Function(ffi.Pointer<ffi.Void>)>('free');

  static final _createJob = _k32.lookupFunction<
      ffi.IntPtr Function(ffi.Pointer<ffi.Void>, ffi.Pointer<ffi.Uint16>),
      int Function(ffi.Pointer<ffi.Void>, ffi.Pointer<ffi.Uint16>)>('CreateJobObjectW');
  static final _setInfo = _k32.lookupFunction<
      ffi.Int32 Function(ffi.IntPtr, ffi.Int32, ffi.Pointer<ffi.Void>, ffi.Uint32),
      int Function(int, int, ffi.Pointer<ffi.Void>, int)>('SetInformationJobObject');
  static final _assign = _k32.lookupFunction<
      ffi.Int32 Function(ffi.IntPtr, ffi.IntPtr),
      int Function(int, int)>('AssignProcessToJobObject');
  static final _currentProcess = _k32.lookupFunction<
      ffi.IntPtr Function(), int Function()>('GetCurrentProcess');

  static bool _installed = false;
  static bool get installed => _installed;

  /// 启动时调用一次。失败一律静默降级（例如进程已被别的 job 管理），不影响主流程。
  static bool install() {
    if (!Platform.isWindows || _installed) return _installed;
    try {
      final job = _createJob(
        ffi.nullptr.cast<ffi.Void>(),
        ffi.nullptr.cast<ffi.Uint16>(),
      );
      if (job == 0) return false;

      final info = _malloc(_infoSize);
      try {
        final bytes = info.cast<ffi.Uint8>().asTypedList(_infoSize);
        for (var i = 0; i < _infoSize; i++) {
          bytes[i] = 0;
        }
        // JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags 在偏移 16（第 4 个 DWORD）
        info.cast<ffi.Uint32>()[4] = _killOnJobClose;
        if (_setInfo(job, _jobObjectExtendedLimitInformation, info, _infoSize) == 0) {
          return false;
        }
      } finally {
        _free(info);
      }

      if (_assign(job, _currentProcess()) == 0) return false;

      // 故意不 CloseHandle(job)：持有到进程结束，句柄关闭的瞬间触发 KILL_ON_JOB_CLOSE。
      _installed = true;
      return true;
    } catch (_) {
      return false;
    }
  }
}
