import MarkdownIt from 'markdown-it'
import Prism from 'prismjs'

// 导入代码高亮主题
import 'prismjs/themes/prism-okaidia.css'

// 引入需要的 Prism.js 语言高亮模块
import 'prismjs/components/prism-javascript'
import 'prismjs/components/prism-css'
import 'prismjs/components/prism-json'
import 'prismjs/components/prism-bash'
import 'prismjs/components/prism-python'
import 'prismjs/components/prism-java'
import 'prismjs/components/prism-sql'
import 'prismjs/components/prism-typescript'
import 'prismjs/components/prism-markup'

// 初始化 MarkdownIt
const md: MarkdownIt = new MarkdownIt({
  html: true, // 允许渲染 HTML 标签。
  xhtmlOut: false, // 不输出 XHTML 格式
  breaks: true, // 将 \n 转换为 <br>
  langPrefix: 'language-', // 代码块语言前缀
  linkify: true, // 自动转换链接
  typographer: true, // 优化排版
  // 自定义代码高亮
  highlight(str: string, lang: string) {
    const language = lang && Prism.languages[lang] ? lang : 'plaintext'
    try {
      const grammar = Prism.languages[language]
      if (!grammar) throw new Error('未找到对应的 Prism 语法高亮支持')
      const html = Prism.highlight(str, grammar as Prism.Grammar, language)
      return `<pre class="code-block" data-lang="${language}"><code class="language-${language}">${html}</code></pre>`
    } catch {
      // 如果高亮失败，则回退到默认渲染规则
      const html = md.utils.escapeHtml(str)
      return `<pre class="code-block" data-lang="plaintext"><code class="language-plaintext">${html}</code></pre>`
    }
  }
})

// 重写 fence 规则以确保代码块始终被高亮容器包裹
const defaultFence =
  md.renderer.rules.fence ??
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))

md.renderer.rules.fence = (
  tokens: any[],
  idx: number,
  options: any,
  env: any,
  self: any
) => {
  const token = tokens[idx]
  const lang = token.info.trim()
  if (lang && Prism.languages[lang]) {
    try {
      const grammar = Prism.languages[lang]
      if (!grammar) throw new Error('Missing prism grammar')
      const html = Prism.highlight(token.content, grammar as Prism.Grammar, lang)
      return `<pre class="code-block" data-lang="${lang}"><code class="language-${lang}">${html}</code></pre>`
    } catch {
      // 如果高亮失败，则回退到默认渲染规则
      return defaultFence(tokens, idx, options, env, self)
    }
  }
  return defaultFence(tokens, idx, options, env, self)
}



const MAX_CACHE = 200
const htmlCache = new Map<string, string>()

/**
 * 渲染 Markdown 文本为 HTML（带轻量缓存）
 * - 适合“流式更新最后一条消息”的场景：旧消息即使被重复渲染也不会重复解析。
 * @param {string} text - Markdown 原文
 * @param {string} [cacheKey] - 可选：业务侧传入稳定 key（如 message.id）
 */
export function renderMarkdownCached(text: string, cacheKey?: string): string {
  if (!text) return ''
  const key = cacheKey ? `${cacheKey}::${text}` : text
  const hit = htmlCache.get(key)
  if (hit !== undefined) return hit

  const html = md.render(text)
  htmlCache.set(key, html)
  // 如果缓存条目数量超过 MAX_CACHE（最多允许的缓存条数），则删除最早插入的那一条缓存（FIFO），以防止内存泄漏或缓存无限增长。
  if (htmlCache.size > MAX_CACHE) {
    const firstKey = htmlCache.keys().next().value as string | undefined
    if (firstKey) htmlCache.delete(firstKey)
  }

  return html
}

// 默认导出 markdown-it 实例，方便在其他地方进行扩展
export default md
