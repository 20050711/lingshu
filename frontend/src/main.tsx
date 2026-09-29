import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { IconContext } from '@phosphor-icons/react'
import App from './App'
import './styles/theme.css'

// 全局 antd 中文 locale（Modal 确定/取消、分页、空数据等内置文案；未配则默认英文 OK/Cancel）
// 深色玻璃主题：darkAlgorithm 打底 + token 对齐 styles/theme.css 的设计令牌，
// 避免"自研 CSS 与 antd 两套视觉"（表格/表单/弹窗/下拉随之统一）。
// IconContext（2026-09-18）：兜底默认值——即便某处直接 import Phosphor 图标（未走 components/Icon
// 封装）也拿到 Light 细线 + 16px，杜绝粗细/尺寸再次散掉
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          colorPrimary: '#5b8cff',
          colorInfo: '#5b8cff',
          colorSuccess: '#3ddc97',
          colorWarning: '#f5c451',
          colorError: '#ff6b6b',
          colorBgBase: '#050505',
          colorBgContainer: 'rgba(255, 255, 255, 0.035)',
          colorBgElevated: '#14171f',
          colorBorder: 'rgba(255, 255, 255, 0.12)',
          colorBorderSecondary: 'rgba(255, 255, 255, 0.08)',
          colorText: '#f2f5fa',
          colorTextSecondary: '#a9b4c6',
          colorTextTertiary: '#6e7a8d',
          colorTextQuaternary: '#525c6b',
          borderRadius: 12,
          fontSize: 13,
          fontFamily: '"PingFang SC", "HarmonyOS Sans SC", "Source Han Sans SC", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, -apple-system, sans-serif',
        },
        components: {
          Table: {
            headerBg: 'rgba(255, 255, 255, 0.04)',
            rowHoverBg: 'rgba(255, 255, 255, 0.06)',
            borderColor: 'rgba(255, 255, 255, 0.08)',
          },
          Modal: { contentBg: 'rgba(16, 19, 26, 0.96)' },
          Select: { optionSelectedBg: 'rgba(91, 140, 255, 0.16)' },
          Tabs: { itemSelectedColor: '#9dbaff', inkBarColor: '#5b8cff' },
          Button: { primaryShadow: 'none' },
        },
      }}
    >
      <IconContext.Provider value={{ weight: 'light', size: 16 }}>
        <App />
      </IconContext.Provider>
    </ConfigProvider>
  </React.StrictMode>,
)
