import { createRouter, createWebHistory } from 'vue-router'
import KnowledgeBaseRoom from '../views/KnowledgeBaseRoom.vue'
import LoginView from '../views/LoginView.vue'
import NotebookRoom from '../views/NotebookRoom.vue'
import ProfileView from '../views/ProfileView.vue'
import StudyRoom from '../views/StudyRoom.vue'
import { getAccessToken } from '../api/token'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/study-room'
    },
    {
      path: '/study-room',
      component: StudyRoom
    },
    {
      path: '/knowledge-base',
      component: KnowledgeBaseRoom
    },
    {
      path: '/profile',
      component: ProfileView
    },
    {
      path: '/login',
      component: LoginView
    },
    {
      path: '/notebook',
      component: NotebookRoom
    }
  ]
})

// 全局前置守卫：未登录拦截并记录跳转来源
router.beforeEach((to) => {
  if (to.path === '/login') {
    return true
  }
  const token = getAccessToken()
  if (!token) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  return true
})

export default router
