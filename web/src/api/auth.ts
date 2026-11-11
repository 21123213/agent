import request from "@/utils/request";
import type { UserInfo } from "@/utils/auth";

export type LoginResponse = {
  access_token: string;
  token_type: string;
  user_info: UserInfo;
};

export type MessageResponse = {
  success: boolean;
  message: string;
  data?: Record<string, unknown>;
};

export const login = (data: { username: string; password: string }) =>
  request.post<any, LoginResponse>("/api/auth/login", data);

export const register = (data: {
  username: string;
  password: string;
  phone?: string;
}) => request.post<any, MessageResponse>("/api/auth/register", data);

export const resetPassword = (data: {
  username: string;
  phone: string;
  new_password: string;
}) => request.post<any, MessageResponse>("/api/auth/reset-password", data);

export const getCurrentUser = () => request.get<any, UserInfo>("/api/auth/me");

export const logoutApi = () =>
  request.post<any, MessageResponse>("/api/auth/logout");
