import { useEffect, useState } from "react";
import { Avatar, Dropdown, Layout, Tabs, message } from "antd";
import { LogoutOutlined, UserOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { getUserInfo, logout as clearLocal } from "@/utils/auth";
import { logoutApi } from "@/api/auth";
import { getAdminCapabilities } from "@/api/admin";
import TabQa from "./TabQa";
import TabAskData from "./TabAskData";
import TabAdminUsers from "./TabAdminUsers";

const { Header, Content } = Layout;

/**
 * 主导航。
 * RAG Online 已并入「智能问答」（背后是 Reasoner 图 + rag_search），
 * 不再单独暴露「Agent」Tab；纯推理/旅游 API 仍保留，只是无独立入口。
 */
export default function Main() {
  const navigate = useNavigate();
  const userInfo = getUserInfo();
  const [activeKey, setActiveKey] = useState("1");
  const [askMounted, setAskMounted] = useState(false);
  const [adminMounted, setAdminMounted] = useState(false);
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    if (activeKey === "2") setAskMounted(true);
    if (activeKey === "3") setAdminMounted(true);
  }, [activeKey]);

  useEffect(() => {
    getAdminCapabilities()
      .then((c) => setIsAdmin(Boolean(c.is_admin)))
      .catch(() => setIsAdmin(Number(userInfo?.role_id) === 1));
  }, [userInfo?.role_id]);

  const handleLogout = async () => {
    try {
      await logoutApi();
    } catch {
      /* ignore */
    }
    clearLocal();
    message.success("已登出");
    navigate("/login");
  };

  const tabItems = [
    { key: "1", label: "智能问答" },
    { key: "2", label: "智能问数" },
    ...(isAdmin ? [{ key: "3", label: "用户管理" }] : []),
  ];

  return (
    <Layout style={{ height: "100vh" }}>
      <Header
        style={{
          background: "#fff",
          padding: "0 24px",
          boxShadow: "0 2px 8px rgba(0,0,0,0.06)",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <div
          style={{
            fontFamily: '"Instrument Serif", Georgia, serif',
            fontSize: 26,
            color: "#1a6b5c",
            letterSpacing: "0.02em",
          }}
        >
          Agents
        </div>
        <Dropdown
          menu={{
            items: [
              {
                key: "logout",
                icon: <LogoutOutlined />,
                label: "退出登录",
                onClick: handleLogout,
              },
            ],
          }}
          placement="bottomRight"
        >
          <div
            style={{
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            <Avatar icon={<UserOutlined />} style={{ background: "#1a6b5c" }} />
            <span>{userInfo?.username}</span>
          </div>
        </Dropdown>
      </Header>
      <Content
        style={{
          background: "#eef3f1",
          height: "calc(100vh - 64px)",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <Tabs
          activeKey={activeKey}
          onChange={setActiveKey}
          items={tabItems}
          style={{ padding: "0 24px", flexShrink: 0 }}
          className="main-nav-tabs"
        />
        <div
          style={{
            flex: 1,
            minHeight: 0,
            overflow: "hidden",
            display: activeKey === "1" ? "block" : "none",
            padding: "0 24px 16px",
          }}
        >
          <TabQa />
        </div>
        {askMounted && (
          <div
            style={{
              flex: 1,
              minHeight: 0,
              overflow: "hidden",
              display: activeKey === "2" ? "block" : "none",
              padding: "0 24px 16px",
            }}
          >
            <TabAskData />
          </div>
        )}
        {adminMounted && isAdmin && (
          <div
            style={{
              flex: 1,
              minHeight: 0,
              overflow: "hidden",
              display: activeKey === "3" ? "block" : "none",
              padding: "0 24px 16px",
            }}
          >
            <TabAdminUsers />
          </div>
        )}
      </Content>
    </Layout>
  );
}
