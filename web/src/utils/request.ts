import axios, { AxiosInstance, AxiosResponse } from "axios";
import { message } from "antd";

const request: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "",
  timeout: 30000,
  headers: { "Content-Type": "application/json" },
});

request.interceptors.request.use((config) => {
  const token = localStorage.getItem("access_token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

request.interceptors.response.use(
  (response: AxiosResponse) => response.data,
  (error) => {
    if (error.response) {
      const { status, data } = error.response;
      const detail = data?.detail || data?.message || "请求失败";
      if (status === 401) {
        message.error("未授权，请重新登录");
        localStorage.removeItem("access_token");
        localStorage.removeItem("user_info");
        if (!window.location.pathname.includes("/login")) {
          window.location.href = "/login";
        }
      } else if (status === 501) {
        message.warning(typeof detail === "string" ? detail : "接口尚未实现");
      } else {
        message.error(typeof detail === "string" ? detail : "请求失败");
      }
    } else {
      message.error("网络错误，请检查后端是否启动");
    }
    return Promise.reject(error);
  }
);

export default request;
