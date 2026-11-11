import { useState } from "react";
import { Button, Form, Input, message } from "antd";
import { LockOutlined, UserOutlined } from "@ant-design/icons";
import { Link, useNavigate } from "react-router-dom";
import { login } from "@/api/auth";
import { setToken, setUserInfo } from "@/utils/auth";

export default function Login() {
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true);
    try {
      const res = await login(values);
      setToken(res.access_token);
      setUserInfo(res.user_info);
      message.success("登录成功");
      navigate("/main");
    } catch {
      /* interceptor */
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">Agents</div>
        <p className="auth-sub">多 Agent 工作台 · 登录</p>
        <Form name="login" onFinish={onFinish} size="large" autoComplete="off">
          <Form.Item
            name="username"
            rules={[{ required: true, message: "请输入用户名" }]}
          >
            <Input prefix={<UserOutlined />} placeholder="用户名" />
          </Form.Item>
          <Form.Item
            name="password"
            rules={[{ required: true, message: "请输入密码" }]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="密码" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={loading} block>
              登录
            </Button>
          </Form.Item>
        </Form>
        <div className="auth-footer">
          <Link to="/forgot-password">忘记密码</Link>
          <span style={{ margin: "0 10px", opacity: 0.4 }}>|</span>
          还没有账号？ <Link to="/register">立即注册</Link>
        </div>
      </div>
    </div>
  );
}
