const AVATAR_FILE: Record<string, string> = {
  airi: '桃井爱莉.jpg',
  akito: '东云彰人.jpg',
  an: '白石杏.jpg',
  emu: '凤笑梦.jpg',
  ena: '东云绘名.jpg',
  haruka: '桐谷遥.jpg',
  honami: '望月穗波.jpg',
  ichika: '星乃一歌.jpg',
  kanade: '宵崎奏.jpg',
  kohane: '小豆泽心羽.jpg',
  mafuyu: '朝比奈真冬.jpg',
  minori: '花里实乃理.jpg',
  mizuki: '晓山瑞希.jpg',
  nene: '草薙宁宁.jpg',
  rui: '神代类.jpg',
  saki: '天马咲希.jpg',
  shiho: '日野森志步.jpg',
  shizuku: '日野森雫.jpg',
  toya: '青柳冬弥.jpg',
  tsukasa: '天马司.jpg',
};

interface CharacterAvatarProps {
  character: string;
  name: string;
}

/** 角色 key 是接口权威；中文文件名只在这里映射，不从展示名反推。 */
export default function CharacterAvatar({ character, name }: CharacterAvatarProps) {
  const file = AVATAR_FILE[character];

  if (!file) return <span className={`avatar avatar-fallback ${character}`}>{name.charAt(0)}</span>;

  return (
    <span className={`avatar avatar-image ${character}`} aria-label={name}>
      <img src={encodeURI(`/avatars/${file}`)} alt="" />
      <span className="avatar-initial" aria-hidden="true">{name.charAt(0)}</span>
    </span>
  );
}
