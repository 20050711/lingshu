/**
 * 用户须知与使用规范 / API 并发限制说明——双卡片（置于快速上手之前，醒目展示）。
 */
import Icon from '../../components/Icon'
import { RATE_LIMIT, USER_NOTICE } from './notice'

function NoticeCard({ data }: { data: typeof USER_NOTICE }) {
  return (
    <div className="gd-notice-card">
      <div className="gd-notice-head">
        <span className="gd-notice-icon"><Icon as={data.icon} size={22} /></span>
        <h3>{data.title}</h3>
      </div>
      <ul className="gd-notice-items">
        {data.items.map((it, i) => (
          <li key={i}>
            <span className="gd-notice-t">{it.t}</span>
            <span className="gd-notice-d">{it.d}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export default function NoticeCards() {
  return (
    <section className="gd-notice-wrap">
      <NoticeCard data={USER_NOTICE} />
      <NoticeCard data={RATE_LIMIT} />
    </section>
  )
}
