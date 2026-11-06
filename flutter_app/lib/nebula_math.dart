import 'dart:math' as math;

/// 3D 向量与旋转/透视投影。零依赖，纯 Dart。
class Vec3 {
  const Vec3(this.x, this.y, this.z);
  final double x;
  final double y;
  final double z;

  Vec3 operator +(Vec3 other) => Vec3(x + other.x, y + other.y, z + other.z);
  Vec3 operator -(Vec3 other) => Vec3(x - other.x, y - other.y, z - other.z);
  Vec3 operator *(double k) => Vec3(x * k, y * k, z * k);
  double get length => math.sqrt(x * x + y * y + z * z);

  Vec3 lerp(Vec3 other, double t) => this + (other - this) * t;
}

/// 先绕 Y 轴 yaw，再绕 X 轴 pitch（拖拽翻滚）。
Vec3 rotate(Vec3 p, double yaw, double pitch) {
  final cy = math.cos(yaw), sy = math.sin(yaw);
  final x1 = p.x * cy - p.z * sy;
  final z1 = p.x * sy + p.z * cy;
  final cp = math.cos(pitch), sp = math.sin(pitch);
  return Vec3(x1, p.y * cp - z1 * sp, p.y * sp + z1 * cp);
}

/// 绕簇心自转（星系盘缓慢旋转）。
Vec3 spinAround(Vec3 p, Vec3 center, double angle) {
  final dx = p.x - center.x, dz = p.z - center.z;
  final c = math.cos(angle), s = math.sin(angle);
  return Vec3(center.x + dx * c - dz * s, p.y, center.z + dx * s + dz * c);
}

class Projected {
  const Projected(this.x, this.y, this.depth, this.scale);
  final double x;
  final double y;
  final double depth;
  final double scale;
}

/// 透视投影：z 越大越远，缩放权重 = focal/(focal+z)。
Projected project(Vec3 p, {required double focal, required double cx, required double cy, required double zoom}) {
  final w = focal / math.max(120.0, focal + p.z);
  return Projected(cx + p.x * w * zoom, cy + p.y * w * zoom, p.z, w * zoom);
}

/// 稳定哈希：同一记忆 ID 在任何设备上落在同一位置（跨设备可复现）。
double hashUnit(String text, [int salt = 0]) {
  var h = 2166136261 ^ salt;
  for (final unit in text.codeUnits) {
    h ^= unit;
    h = (h * 16777619) & 0x7fffffff;
  }
  return (h & 0xffffff) / 0xffffff;
}
