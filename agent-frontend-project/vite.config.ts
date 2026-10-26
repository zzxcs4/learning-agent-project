import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // 根据当前工作目录中的 `mode` 加载 .env 文件
  // 设置第三个参数为 '' 来加载所有环境变量，而不仅仅是 `VITE_` 开头的变量。
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [vue()],
    resolve: {
      alias: {
        // 设置路径别名
        '@': path.resolve(__dirname, './src')
      }
    },
    server: {
      host: '0.0.0.0', // 允许外部访问
      port: 8080,      // 设置端口
      proxy: {
        // 配置代理
        '/api': {
          target: env.VITE_API_PROXY_TARGET || 'http://localhost:8000', // 代理目标地址
          changeOrigin: true, // 允许跨域
          rewrite: (path) => path.replace(/^\/api/, '') // 重写路径
        }
      }
    }
  }
})
