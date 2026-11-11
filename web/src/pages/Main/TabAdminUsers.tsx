/**
 * 用户管理 Tab（仅 role=admin 可见）
 *
 * 可调整用户的 role_id / unit_id / is_active。
 * 注册默认 viewer；由此处提权。
 */
import { useEffect, useState } from "react";
import {
  Select,
  Switch,
  Table,
  Typography,
  message,
  Space,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  getAdminCapabilities,
  listAdminRoles,
  listAdminUnits,
  listAdminUsers,
  patchAdminUser,
  type AdminRole,
  type AdminUnit,
  type AdminUser,
} from "@/api/admin";

const { Title, Text } = Typography;

export default function TabAdminUsers() {
  const [allowed, setAllowed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [roles, setRoles] = useState<AdminRole[]>([]);
  const [units, setUnits] = useState<AdminUnit[]>([]);

  const reload = async () => {
    setLoading(true);
    try {
      const cap = await getAdminCapabilities();
      setAllowed(Boolean(cap.is_admin));
      if (!cap.is_admin) {
        setUsers([]);
        return;
      }
      const [u, r, un] = await Promise.all([
        listAdminUsers(),
        listAdminRoles(),
        listAdminUnits(),
      ]);
      setUsers(u || []);
      setRoles(r || []);
      setUnits(un || []);
    } catch {
      setAllowed(false);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    reload();
  }, []);

  const onPatch = async (
    userId: number,
    body: { role_id?: number; unit_id?: number; is_active?: boolean }
  ) => {
    try {
      await patchAdminUser(userId, body);
      message.success("已更新");
      await reload();
    } catch {
      /* */
    }
  };

  if (!loading && !allowed) {
    return (
      <div style={{ padding: 24, background: "#fff", borderRadius: 8 }}>
        <Text type="secondary">仅管理员可访问用户管理。</Text>
      </div>
    );
  }

  const columns: ColumnsType<AdminUser> = [
    { title: "ID", dataIndex: "id", width: 72 },
    { title: "用户名", dataIndex: "username", width: 140 },
    {
      title: "角色",
      dataIndex: "role_id",
      width: 160,
      render: (roleId: number, row) => (
        <Select
          style={{ width: 140 }}
          value={roleId}
          options={roles.map((r) => ({
            value: r.id,
            label: `${r.name}(${r.code})`,
          }))}
          onChange={(v) => onPatch(row.id, { role_id: v })}
        />
      ),
    },
    {
      title: "租户 unit",
      dataIndex: "unit_id",
      width: 180,
      render: (unitId: number, row) => (
        <Select
          style={{ width: 160 }}
          value={unitId}
          options={units.map((u) => ({
            value: u.id,
            label: `${u.name}(${u.code})`,
          }))}
          onChange={(v) => onPatch(row.id, { unit_id: v })}
        />
      ),
    },
    {
      title: "启用",
      dataIndex: "is_active",
      width: 100,
      render: (active: boolean, row) => (
        <Switch
          checked={active}
          onChange={(v) => onPatch(row.id, { is_active: v })}
        />
      ),
    },
    {
      title: "手机",
      dataIndex: "phone",
      render: (p: string | null | undefined) => p || <Tag>无</Tag>,
    },
  ];

  return (
    <div
      style={{
        height: "100%",
        background: "#fff",
        borderRadius: 8,
        padding: 16,
        overflow: "auto",
      }}
    >
      <Space direction="vertical" size={4} style={{ marginBottom: 12 }}>
        <Title level={5} style={{ margin: 0 }}>
          用户管理
        </Title>
        <Text type="secondary">
          调整 role / unit。注册默认访客(viewer)；问数行权限跟 unit，admin
          可跨 unit 查数。
        </Text>
      </Space>
      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={users}
        pagination={{ pageSize: 20 }}
        size="middle"
      />
    </div>
  );
}
