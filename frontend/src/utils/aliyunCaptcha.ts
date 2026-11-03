/**
 * 阿里云验证码 2.0（人机验证 / 滑块）前端封装。
 *
 * 官方接入方式是「动态引入 JS + 全局配置 + initAliyunCaptcha」，没有官方 npm 包，
 * 所以这里用脚本注入的方式封装。
 *
 * 用法：
 *   const captcha = useAliyunCaptcha({ prefix, sceneId })
 *   const param = await captcha.run()   // 弹滑块；用户通过返回 captchaVerifyParam，
 *                                       // 用户取消/失败返回空串
 *
 * 后端必须**再校验一次**（见 backend/app/captcha_provider.py）—— 前端返回的
 * 参数是攻击者可控的，只信前端等于没做验证。
 */
import { ref } from 'vue'

/** 阿里云验证码 JS 资源地址（官方 CDN，不可本地部署） */
const SDK_URL = 'https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js'

export interface AliyunCaptchaInstance {
  /** 主动唤起验证码（popup 模式下用于程序化触发） */
  execute?: () => void
  reset?: () => void
}

interface AliyunCaptchaOptions {
  SceneId: string
  mode: 'popup' | 'embed'
  element: string
  button: string
  slideStyle: { width: number; height: number }
  /** 加密模式：服务端动态生成的加密场景串（防 SceneId 泄露被白嫖额度） */
  EncryptedSceneId?: string
  success: (captchaVerifyParam: string) => void
  fail: (result: unknown) => void
  getInstance: (instance: AliyunCaptchaInstance) => void
  onError?: (err: unknown) => void
}

declare global {
  interface Window {
    AliyunCaptchaConfig?: { region: string; prefix: string }
    initAliyunCaptcha?: (options: AliyunCaptchaOptions) => void
  }
}

/** SDK 脚本加载：整个页面只真正加载一次 */
let scriptPromise: Promise<void> | null = null

function loadSdk(): Promise<void> {
  if (scriptPromise) return scriptPromise
  scriptPromise = new Promise<void>((resolve, reject) => {
    if (window.initAliyunCaptcha) {
      resolve()
      return
    }
    const script = document.createElement('script')
    script.src = SDK_URL
    script.async = true
    script.onload = () => resolve()
    script.onerror = () => {
      scriptPromise = null
      reject(new Error('阿里云验证码 JS 加载失败'))
    }
    document.head.appendChild(script)
  })
  return scriptPromise
}

export interface AliyunCaptchaConfig {
  /** 身份标（控制台概览页获取） */
  prefix: string | (() => string)
  /** 场景 ID（控制台场景管理获取）；加密模式下由服务端动态下发 */
  sceneId: string | (() => string)
  /**
   * 获取加密场景串（EncryptedSceneId）。
   *
   * 加密模式下服务端会用 ekey 生成一次性短效串 —— SceneId 明文进 JS 的话，
   * 谁都能拿走白嫖账号的验证码额度。返回空串表示走非加密模式。
   */
  encryptedSceneId?: () => Promise<string>
}

export interface UseAliyunCaptcha {
  /** 执行一次人机验证；返回 captchaVerifyParam，用户取消/失败时返回空串 */
  run: () => Promise<string>
  /** 配置是否齐全（不保证 SDK 已加载） */
  ready: () => boolean
  /** 初始化 / 加载过程中的错误信息 */
  error: ReturnType<typeof ref<string>>
}

export function useAliyunCaptcha(config: AliyunCaptchaConfig): UseAliyunCaptcha {
  const error = ref('')

  /** 配置可能是静态值也可能是 getter —— 短信状态是异步拉取的，配置到位前可能为空 */
  const readPrefix = (): string =>
    typeof config.prefix === 'function' ? config.prefix() : config.prefix
  const readSceneId = (): string =>
    typeof config.sceneId === 'function' ? config.sceneId() : config.sceneId

  /** 当前挂起的等待者（initAliyunCaptcha 的回调是固定的，用它中转多次调用） */
  let pending: ((value: string) => void) | null = null
  let instance: AliyunCaptchaInstance | null = null
  let initPromise: Promise<AliyunCaptchaInstance | null> | null = null

  /** 初始化一次，之后复用实例（官方要求不要重复 init） */
  async function ensure(): Promise<AliyunCaptchaInstance | null> {
    if (instance) return instance
    if (initPromise) return initPromise
    const prefix = readPrefix()
    if (!prefix) {
      error.value = '验证码未配置'
      return null
    }
    initPromise = (async () => {
      try {
        window.AliyunCaptchaConfig = { region: 'cn', prefix }
        await loadSdk()
        if (!window.initAliyunCaptcha) {
          error.value = '验证码 SDK 未就绪'
          return null
        }
        // 加密模式：向服务端要一次 EncryptedSceneId（短效，一次一取）
        let encryptedSceneId = ''
        let sceneId = readSceneId()
        if (config.encryptedSceneId) {
          try {
            encryptedSceneId = await config.encryptedSceneId()
          } catch (e) {
            console.warn('[aliyun-captcha] fetch encrypted scene id failed', e)
          }
        }
        // 加密模式下明文 SceneId 可能被服务端刻意置空 —— 此时加密串就是唯一凭据
        if (!sceneId && !encryptedSceneId) {
          error.value = '验证码未配置'
          return null
        }
        return await new Promise<AliyunCaptchaInstance | null>((resolve) => {
          window.initAliyunCaptcha?.({
            SceneId: sceneId,
            ...(encryptedSceneId ? { EncryptedSceneId: encryptedSceneId } : {}),
            mode: 'popup',
            // 这两个元素必须存在于页面上（可以隐藏），否则部分浏览器不渲染验证码
            element: '#aliyun-captcha-element',
            button: '#aliyun-captcha-button',
            slideStyle: { width: 320, height: 40 },
            success: (captchaVerifyParam: string) => {
              pending?.(captchaVerifyParam)
              pending = null
            },
            fail: () => {
              pending?.('')
              pending = null
            },
            getInstance: (inst) => {
              instance = inst
              resolve(inst)
            },
            onError: (err) => {
              error.value = '验证码初始化失败'
              console.warn('[aliyun-captcha] init error', err)
              resolve(null)
            },
          })
          // 兜底：3 秒内没拿到实例也放行，避免 SDK 异常时整个发码流程卡死
          setTimeout(() => resolve(null), 3000)
        })
      } catch (e) {
        error.value = e instanceof Error ? e.message : '验证码初始化失败'
        return null
      }
    })()
    return initPromise
  }

  /** 弹滑块并等待结果 */
  async function run(): Promise<string> {
    // 加密模式下 EncryptedSceneId 有有效期，不能复用同一个实例 ——
    // 每次重新 init，拿一个新鲜的短效串。
    if (config.encryptedSceneId) {
      instance = null
      initPromise = null
    }
    const inst = await ensure()
    if (!inst) return ''
    return new Promise<string>((resolve) => {
      let settled = false
      const finish = (value: string) => {
        if (settled) return
        settled = true
        resolve(value)
      }
      pending = finish
      try {
        inst.execute?.()
      } catch {
        finish('')
      }
      // 用户可能直接关掉弹窗而不触发 fail —— 兜底超时
      setTimeout(() => {
        pending = null
        finish('')
      }, 120000)
    })
  }

  return {
    run,
    // 加密模式下服务端会刻意置空明文 SceneId，此时只靠 prefix 判断可用性
    ready: () => !!(readPrefix() && (readSceneId() || !!config.encryptedSceneId)),
    error,
  }
}
