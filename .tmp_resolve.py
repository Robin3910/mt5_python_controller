import io

p = 'frontend/src/views/GroupsView.vue'
s = io.open(p, encoding='utf-8').read()

i = s.index('<<<<<<< HEAD')
seg_end = s.index('>>>>>>> dev', i) + len('>>>>>>> dev')
seg = s[i:seg_end]
k = seg.index('=======')
head = seg[len('<<<<<<< HEAD'):k]
dev = seg[k + len('======='):-len('>>>>>>> dev')]

# 采用 v4 的卡片结构（字段更全），但保留 dev 的移动端布局类 group-actions
head = head.replace('<div class="list-card-actions">', '<div class="list-card-actions group-actions">')

s = s[:i] + head + s[seg_end:]
io.open(p, 'w', encoding='utf-8', newline='').write(s)
print('resolved GroupsView.vue')
