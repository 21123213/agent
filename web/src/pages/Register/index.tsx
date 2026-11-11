import { useState } from "react";
import { Button, Form, Input, message } from "antd";
import { LockOutlined, PhoneOutlined, UserOutlined } from "@ant-design/icons";
import { Link, useNavigate } from "react-router-dom";
import { register } from "@/api/auth";

export default function Register() {
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const onFinish = async (values: {
    username: string;
    password: string;
    phone?: string;
  }) => {
    setLoading(true);
    try {
      const phone = values.phone?.trim() || undefined;
      await register({
        username: values.username,
        password: values.password,
        phone,
      });
      message.success("注册成功，请登录");
      navigate("/login");
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
        <p className="auth-sub">创建账号 · 手机号选填</p>
        <Form name="register" onFinish={onFinish} size="large" autoComplete="off">
          <Form.Item
            name="username"
            rules={[
              { required: true, message: "请输入用户名" },
              { min: 3, message: "至少 3 个字符" },
            ]}
          >
            <Input prefix={<UserOutlined />} placeholder="用户名" />
          </Form.Item>
          <Form.Item
            name="password"
            rules={[
              { required: true, message: "请输入密码" },
              { min: 6, message: "至少 6 位" },
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="密码" />
          </Form.Item>
          <Form.Item
            name="confirm"
            dependencies={["password"]}
            rules={[
              { required: true, message: "请确认密码" },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue("password") === value) {
                    return Promise.resolve();
                  }
                  return Promise.reject(new Error("两次密码不一致"));
                },
              }),
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="确认密码" />
          </Form.Item>
          <Form.Item
            name="phone"
            rules={[
              {
                pattern: /^1[3-9]\d{9}$/,
                message: "手机号格式不正确",
              },
            ]}
          >
            <Input prefix={<PhoneOutlined />} placeholder="手机号（选填，用于找回密码）" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={loading} block>
              注册
            </Button>
          </Form.Item>
        </Form>
        <div className="auth-footer">
          已有账号？ <Link to="/login">立即登录</Link>
        </div>
      </div>
    </div>
  );
}
