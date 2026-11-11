import { useState } from "react";
import { Button, Form, Input, message } from "antd";
import { LockOutlined, PhoneOutlined, UserOutlined } from "@ant-design/icons";
import { Link, useNavigate } from "react-router-dom";
import { resetPassword } from "@/api/auth";

export default function ForgotPassword() {
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const onFinish = async (values: {
    username: string;
    phone: string;
    new_password: string;
  }) => {
    setLoading(true);
    try {
      await resetPassword({
        username: values.username,
        phone: values.phone,
        new_password: values.new_password,
      });
      message.success("密码已重置，请登录");
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
        <p className="auth-sub">重置密码 · 需用户名与手机号同时匹配</p>
        <Form name="reset" onFinish={onFinish} size="large" autoComplete="off">
          <Form.Item
            name="username"
            rules={[{ required: true, message: "请输入用户名" }]}
          >
            <Input prefix={<UserOutlined />} placeholder="用户名" />
          </Form.Item>
          <Form.Item
            name="phone"
            rules={[
              { required: true, message: "请输入注册时绑定的手机号" },
              { pattern: /^1[3-9]\d{9}$/, message: "手机号格式不正确" },
            ]}
          >
            <Input prefix={<PhoneOutlined />} placeholder="手机号" />
          </Form.Item>
          <Form.Item
            name="new_password"
            rules={[
              { required: true, message: "请输入新密码" },
              { min: 6, message: "至少 6 位" },
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="新密码" />
          </Form.Item>
          <Form.Item
            name="confirm"
            dependencies={["new_password"]}
            rules={[
              { required: true, message: "请确认新密码" },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue("new_password") === value) {
                    return Promise.resolve();
                  }
                  return Promise.reject(new Error("两次密码不一致"));
                },
              }),
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="确认新密码" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={loading} block>
              重置密码
            </Button>
          </Form.Item>
        </Form>
        <div className="auth-footer">
          <Link to="/login">返回登录</Link>
        </div>
      </div>
    </div>
  );
}
