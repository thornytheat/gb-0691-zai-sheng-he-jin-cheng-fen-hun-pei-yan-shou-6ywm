from flask import Blueprint, request, jsonify
from app.pipeline import calculate
from app.numeric import serial
bp=Blueprint('m6',__name__)
@bp.route('/api/m6', methods=['GET','POST'])
def module_6():
    payload=request.get_json(silent=True) or {}
    result=calculate(payload.get('csv'), stop=6, recipe=payload.get('recipe') or payload.get('version'))
    return jsonify(serial(result['stages']['6']))
